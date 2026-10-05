import { useCallback, useEffect, useRef, useState } from 'react';

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

export function TeammatePanel({ token, onSendToChat }: TeammatePanelProps) {
  const [status, setStatus] = useState<TeammateStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
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
        throw new Error(`Failed to load teammate status (${res.status})`);
      }
      const data = (await res.json()) as TeammateStatus;
      setStatus(data);
      setError('');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Error loading teammate status');
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  const handleRunPatrol = async () => {
    if (patrolling || !token) return;
    try {
      setPatrolling(true);
      const res = await fetch('/api/teammate/patrol', {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ force: true }),
      });
      if (!res.ok) {
        throw new Error(`Patrol execution failed (${res.status})`);
      }
      const data = (await res.json()) as PatrolResult;
      setPatrolResult(data);
      void loadStatus();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Patrol run failed');
    } finally {
      setPatrolling(false);
    }
  };

  const handleAction = async (action: 'pause' | 'resume' | 'mute' | 'unmute', value?: unknown) => {
    if (actionBusy || !token) return;
    try {
      setActionBusy(true);
      const res = await fetch('/api/teammate/action', {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ action, value }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.error || `Action ${action} failed`);
      }
      if (action === 'mute') setNewMuteSource('');
      await loadStatus();
    } catch (err) {
      setError(err instanceof Error ? err.message : `Action failed`);
    } finally {
      setActionBusy(false);
    }
  };

  const formatTs = (ts?: string | null) => {
    if (!ts) return '尚未执行 (Not yet run)';
    try {
      return new Date(ts).toLocaleString([], {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      });
    } catch {
      return ts;
    }
  };

  return (
    <div style={{ padding: '16px 24px', display: 'flex', flexDirection: 'column', gap: 20 }}>
      {/* Top Header Card */}
      <div
        style={{
          background: 'var(--card-bg, #1a1b26)',
          border: '1px solid var(--border-color, #2f354a)',
          borderRadius: 8,
          padding: '16px 20px',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          flexWrap: 'wrap',
          gap: 12,
        }}
      >
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <h3 style={{ margin: 0, fontSize: 18, display: 'flex', alignItems: 'center', gap: 8 }}>
              🛡️ Always-On Teammate
            </h3>
            {status?.is_paused ? (
              <span
                style={{
                  background: '#f59e0b22',
                  color: '#f59e0b',
                  border: '1px solid #f59e0b55',
                  padding: '2px 8px',
                  borderRadius: 4,
                  fontSize: 12,
                  fontWeight: 600,
                }}
              >
                ⏸️ 已暂停预警 (Paused)
              </span>
            ) : (
              <span
                style={{
                  background: '#10b98122',
                  color: '#10b981',
                  border: '1px solid #10b98155',
                  padding: '2px 8px',
                  borderRadius: 4,
                  fontSize: 12,
                  fontWeight: 600,
                }}
              >
                🟢 24/7 守护活跃中 (Active)
              </span>
            )}
          </div>
          <p style={{ margin: '6px 0 0', fontSize: 13, color: 'var(--text-secondary, #94a3b8)' }}>
            自动巡检周期: 每 {status?.interval_seconds ? status.interval_seconds / 60 : 5} 分钟 · 防骚扰冷却:{' '}
            {status?.cooldown_seconds ? status.cooldown_seconds / 3600 : 2} 小时 · 上次巡检:{' '}
            {formatTs(status?.last_patrol_at)}
          </p>
        </div>

        {/* Action Controls */}
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <button
            type="button"
            className="primary-button"
            disabled={patrolling || loading}
            onClick={handleRunPatrol}
            style={{
              padding: '6px 14px',
              fontSize: 13,
              fontWeight: 600,
              cursor: patrolling ? 'not-allowed' : 'pointer',
            }}
          >
            {patrolling ? '⚡ 正在巡检中…' : '🛡️ 立即全面巡检 (Run Patrol)'}
          </button>

          {status?.is_paused ? (
            <button
              type="button"
              disabled={actionBusy}
              onClick={() => handleAction('resume')}
              style={{
                padding: '6px 12px',
                fontSize: 13,
                cursor: 'pointer',
                background: '#10b981',
                color: '#fff',
                border: 'none',
                borderRadius: 4,
              }}
            >
              ▶️ 恢复提醒
            </button>
          ) : (
            <>
              <button
                type="button"
                disabled={actionBusy}
                onClick={() => handleAction('pause', 4)}
                style={{
                  padding: '6px 12px',
                  fontSize: 13,
                  cursor: 'pointer',
                  background: 'transparent',
                  color: 'var(--text-secondary, #94a3b8)',
                  border: '1px solid var(--border-color, #2f354a)',
                  borderRadius: 4,
                }}
              >
                ⏸️ 暂停 4h
              </button>
              <button
                type="button"
                disabled={actionBusy}
                onClick={() => handleAction('pause', 24)}
                style={{
                  padding: '6px 12px',
                  fontSize: 13,
                  cursor: 'pointer',
                  background: 'transparent',
                  color: 'var(--text-secondary, #94a3b8)',
                  border: '1px solid var(--border-color, #2f354a)',
                  borderRadius: 4,
                }}
              >
                ⏸️ 暂停 24h
              </button>
            </>
          )}

          <button
            type="button"
            disabled={loading}
            onClick={() => loadStatus()}
            style={{
              padding: '6px 10px',
              fontSize: 13,
              cursor: 'pointer',
              background: 'transparent',
              color: 'var(--text-secondary, #94a3b8)',
              border: '1px solid var(--border-color, #2f354a)',
              borderRadius: 4,
            }}
          >
            🔄 刷新
          </button>
        </div>
      </div>

      {error && (
        <div
          style={{
            background: '#ef444422',
            color: '#ef4444',
            border: '1px solid #ef444455',
            padding: '8px 12px',
            borderRadius: 6,
            fontSize: 13,
          }}
        >
          ⚠️ {error}
        </div>
      )}

      {/* Live Inspection Result Banner */}
      {patrolResult && (
        <div
          style={{
            background: patrolResult.is_healthy ? '#10b98115' : '#ef444415',
            border: `1px solid ${patrolResult.is_healthy ? '#10b98155' : '#ef444455'}`,
            borderRadius: 8,
            padding: '14px 18px',
          }}
        >
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <h4
              style={{
                margin: 0,
                fontSize: 15,
                color: patrolResult.is_healthy ? '#10b981' : '#ef4444',
                display: 'flex',
                alignItems: 'center',
                gap: 6,
              }}
            >
              {patrolResult.is_healthy
                ? '✅ 实时巡检完成：全系统指标健康正常，未发现任何异常'
                : `🚨 实时巡检发现 ${patrolResult.alerts.length} 项异常指标`}
            </h4>
            <span style={{ fontSize: 12, color: 'var(--text-secondary, #94a3b8)' }}>
              {formatTs(patrolResult.timestamp)}
            </span>
          </div>
          {patrolResult.alerts.length > 0 && (
            <div style={{ marginTop: 10, display: 'flex', flexDirection: 'column', gap: 8 }}>
              {patrolResult.alerts.map((alert, idx) => (
                <div
                  key={idx}
                  style={{
                    background: 'var(--card-bg, #1a1b26)',
                    padding: '8px 12px',
                    borderRadius: 6,
                    border: '1px solid var(--border-color, #2f354a)',
                  }}
                >
                  <div style={{ fontWeight: 600, fontSize: 13 }}>
                    [{alert.severity.toUpperCase()}] {alert.title}
                  </div>
                  <div style={{ fontSize: 12, color: 'var(--text-secondary, #cbd5e1)', marginTop: 4 }}>
                    {alert.summary}
                  </div>
                  {alert.suggested_actions && alert.suggested_actions.length > 0 && (
                    <div style={{ display: 'flex', gap: 8, marginTop: 6 }}>
                      {alert.suggested_actions.map((act, aIdx) => (
                        <button
                          key={aIdx}
                          type="button"
                          onClick={() => onSendToChat?.(act.command)}
                          style={{
                            fontSize: 12,
                            padding: '3px 8px',
                            background: '#3b82f622',
                            color: '#60a5fa',
                            border: '1px solid #3b82f655',
                            borderRadius: 4,
                            cursor: 'pointer',
                          }}
                        >
                          👉 快捷运行: {act.command} ({act.label})
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Guardian Modules Matrix */}
      <div>
        <h4 style={{ margin: '0 0 12px', fontSize: 14, color: 'var(--text-secondary, #94a3b8)' }}>
          📋 守护巡检模块矩阵 (Guardian Modules)
        </h4>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: 14 }}>
          {/* Card 1: Disk */}
          <div
            style={{
              background: 'var(--card-bg, #1a1b26)',
              border: '1px solid var(--border-color, #2f354a)',
              borderRadius: 8,
              padding: 14,
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontWeight: 600, fontSize: 14 }}>
              💾 磁盘空间守护 (Disk)
            </div>
            <p style={{ margin: '8px 0 10px', fontSize: 12, color: 'var(--text-secondary, #94a3b8)' }}>
              使用率 &gt; {status?.thresholds.disk_pct ?? 90}% 或可用空间 &lt; {status?.thresholds.disk_gb ?? 3} GB
              时自动告警
            </p>
            <button
              type="button"
              onClick={() => onSendToChat?.('/clean')}
              style={{
                fontSize: 12,
                padding: '4px 8px',
                background: 'transparent',
                color: '#60a5fa',
                border: '1px solid #3b82f655',
                borderRadius: 4,
                cursor: 'pointer',
              }}
            >
              /clean 清理空间
            </button>
          </div>

          {/* Card 2: CPU */}
          <div
            style={{
              background: 'var(--card-bg, #1a1b26)',
              border: '1px solid var(--border-color, #2f354a)',
              borderRadius: 8,
              padding: 14,
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontWeight: 600, fontSize: 14 }}>
              ⚡ CPU 与高耗能进程守护 (CPU)
            </div>
            <p style={{ margin: '8px 0 10px', fontSize: 12, color: 'var(--text-secondary, #94a3b8)' }}>
              系统 1 分钟平均负载比例 &gt; {status?.thresholds.load_ratio ?? 2.0}x 时抓取前列进程告警
            </p>
            <button
              type="button"
              onClick={() => onSendToChat?.('/ps')}
              style={{
                fontSize: 12,
                padding: '4px 8px',
                background: 'transparent',
                color: '#60a5fa',
                border: '1px solid #3b82f655',
                borderRadius: 4,
                cursor: 'pointer',
              }}
            >
              /ps 查看实时进程
            </button>
          </div>

          {/* Card 3: Services */}
          <div
            style={{
              background: 'var(--card-bg, #1a1b26)',
              border: '1px solid var(--border-color, #2f354a)',
              borderRadius: 8,
              padding: 14,
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontWeight: 600, fontSize: 14 }}>
              🛠️ 核心系统服务守护 (Services)
            </div>
            <p style={{ margin: '8px 0 10px', fontSize: 12, color: 'var(--text-secondary, #94a3b8)' }}>
              实时监测 systemd 守护单元：{status?.monitored_services.join(', ') || 'Telegram, Feishu, VPS Computer'}
            </p>
            <button
              type="button"
              onClick={() => onSendToChat?.('/status')}
              style={{
                fontSize: 12,
                padding: '4px 8px',
                background: 'transparent',
                color: '#60a5fa',
                border: '1px solid #3b82f655',
                borderRadius: 4,
                cursor: 'pointer',
              }}
            >
              /status 检查服务
            </button>
          </div>

          {/* Card 4: Logs */}
          <div
            style={{
              background: 'var(--card-bg, #1a1b26)',
              border: '1px solid var(--border-color, #2f354a)',
              borderRadius: 8,
              padding: 14,
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontWeight: 600, fontSize: 14 }}>
              📜 日志突增异常监测 (Logs)
            </div>
            <p style={{ margin: '8px 0 10px', fontSize: 12, color: 'var(--text-secondary, #94a3b8)' }}>
              10 分钟内检测到错误日志 &gt;= {status?.thresholds.error_burst ?? 5} 条时自动触发异常排查
            </p>
            <button
              type="button"
              onClick={() => onSendToChat?.('/diagnose')}
              style={{
                fontSize: 12,
                padding: '4px 8px',
                background: 'transparent',
                color: '#60a5fa',
                border: '1px solid #3b82f655',
                borderRadius: 4,
                cursor: 'pointer',
              }}
            >
              /diagnose 深度诊断
            </button>
          </div>

          {/* Card 5: Git & CI */}
          <div
            style={{
              background: 'var(--card-bg, #1a1b26)',
              border: '1px solid var(--border-color, #2f354a)',
              borderRadius: 8,
              padding: 14,
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontWeight: 600, fontSize: 14 }}>
              📦 Git 仓库与 GitHub CI 守护
            </div>
            <p style={{ margin: '8px 0 10px', fontSize: 12, color: 'var(--text-secondary, #94a3b8)' }}>
              未提交变更积压预警与 GitHub Actions main 分支构建失败主动拦截
            </p>
            <button
              type="button"
              onClick={() => onSendToChat?.('/github_ci')}
              style={{
                fontSize: 12,
                padding: '4px 8px',
                background: 'transparent',
                color: '#60a5fa',
                border: '1px solid #3b82f655',
                borderRadius: 4,
                cursor: 'pointer',
              }}
            >
              /github_ci 查看构建
            </button>
          </div>
        </div>
      </div>

      {/* Muted Sources & Noise Filter */}
      <div
        style={{
          background: 'var(--card-bg, #1a1b26)',
          border: '1px solid var(--border-color, #2f354a)',
          borderRadius: 8,
          padding: '14px 18px',
        }}
      >
        <h4 style={{ margin: '0 0 10px', fontSize: 14, display: 'flex', alignItems: 'center', gap: 8 }}>
          🔇 静音过滤管理 (Noise Filter)
        </h4>
        <p style={{ margin: '0 0 10px', fontSize: 12, color: 'var(--text-secondary, #94a3b8)' }}>
          静音的告警源将不会主动推送预警通知。已静音：
          {status?.muted_sources && status.muted_sources.length > 0 ? '' : ' (暂无)'}
        </p>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
          {status?.muted_sources?.map((source) => (
            <span
              key={source}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 6,
                background: '#374151',
                padding: '3px 8px',
                borderRadius: 4,
                fontSize: 12,
              }}
            >
              <code>{source}</code>
              <button
                type="button"
                onClick={() => handleAction('unmute', source)}
                style={{
                  background: 'none',
                  border: 'none',
                  color: '#ef4444',
                  cursor: 'pointer',
                  fontSize: 12,
                  padding: 0,
                }}
              >
                ✕
              </button>
            </span>
          ))}

          <div style={{ display: 'inline-flex', gap: 6, alignItems: 'center', marginLeft: 6 }}>
            <input
              type="text"
              placeholder="输入告警源如 host.disk"
              value={newMuteSource}
              onChange={(e) => setNewMuteSource(e.target.value)}
              style={{
                padding: '3px 8px',
                fontSize: 12,
                borderRadius: 4,
                border: '1px solid var(--border-color, #2f354a)',
                background: 'var(--input-bg, #0f172a)',
                color: 'inherit',
              }}
            />
            <button
              type="button"
              disabled={!newMuteSource.trim()}
              onClick={() => handleAction('mute', newMuteSource.trim())}
              style={{
                padding: '3px 8px',
                fontSize: 12,
                borderRadius: 4,
                cursor: 'pointer',
                background: 'transparent',
                border: '1px solid var(--border-color, #2f354a)',
                color: 'inherit',
              }}
            >
              + 静音
            </button>
          </div>
        </div>
      </div>

      {/* Recent Alerts Feed */}
      <div>
        <h4 style={{ margin: '0 0 12px', fontSize: 14, color: 'var(--text-secondary, #94a3b8)' }}>
          🕒 最近触发的守护告警 (Recent Alerts)
        </h4>
        {status?.recent_alerts && status.recent_alerts.length > 0 ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {status.recent_alerts.map((alert, idx) => (
              <div
                key={idx}
                style={{
                  background: 'var(--card-bg, #1a1b26)',
                  border: '1px solid var(--border-color, #2f354a)',
                  borderRadius: 8,
                  padding: '12px 16px',
                }}
              >
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span style={{ fontSize: 16 }}>
                      {alert.severity === 'critical' ? '🚨' : alert.severity === 'warning' ? '⚠️' : 'ℹ️'}
                    </span>
                    <strong style={{ fontSize: 14 }}>{alert.title}</strong>
                    <span
                      style={{
                        fontSize: 11,
                        padding: '1px 6px',
                        borderRadius: 3,
                        background:
                          alert.severity === 'critical'
                            ? '#ef444422'
                            : alert.severity === 'warning'
                            ? '#f59e0b22'
                            : '#3b82f622',
                        color:
                          alert.severity === 'critical'
                            ? '#ef4444'
                            : alert.severity === 'warning'
                            ? '#f59e0b'
                            : '#60a5fa',
                      }}
                    >
                      {alert.source}
                    </span>
                  </div>
                  <span style={{ fontSize: 12, color: 'var(--text-secondary, #94a3b8)' }}>
                    {formatTs(alert.created_at)}
                  </span>
                </div>
                <p style={{ margin: '8px 0 0', fontSize: 13, color: 'var(--text-secondary, #cbd5e1)' }}>
                  {alert.summary}
                </p>
              </div>
            ))}
          </div>
        ) : (
          <div
            style={{
              padding: 24,
              textAlign: 'center',
              color: 'var(--text-secondary, #94a3b8)',
              background: 'var(--card-bg, #1a1b26)',
              borderRadius: 8,
              border: '1px solid var(--border-color, #2f354a)',
              fontSize: 13,
            }}
          >
            🎉 暂无告警记录，智能体队友全天候守护中。
          </div>
        )}
      </div>
    </div>
  );
}
