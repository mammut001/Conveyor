import React, { FormEvent, useCallback, useEffect, useRef, useState } from 'react';
import { applyInboxDecision, isStale } from '../approvalFreshness';
import { FormattedText } from './FormattedText';

export type Routine = {
  id: number;
  name: string;
  schedule: string;
  schedule_cron?: string;
  prompt: string;
  deliver: string[];
  enabled: boolean;
  created_at: string;
  updated_at: string;
  last_run_at?: string | null;
  next_run_at?: string | null;
  last_run_status?: string | null;
  consecutive_failures?: number;
};

export type InboxItem = {
  id: number;
  routine_id: number;
  routine_name: string;
  started_at: string;
  finished_at: string;
  status: string;
  output: string;
  approval_id?: string | null;
  delivery?: Record<string, string>;
  read_at?: string | null;
  approval?: {
    id: string;
    status: string;
    expires_at?: string;
  } | null;
};

export type InboxPanelProps = {
  token: string;
  onUnreadChange?: (count: number) => void;
  onApprovalDecided?: () => void;
};

function formatLocalTime(val?: string | null) {
  if (!val) return '—';
  const d = new Date(val);
  return Number.isNaN(d.valueOf())
    ? val
    : d.toLocaleString([], {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      });
}

export function InboxPanel({ token, onUnreadChange, onApprovalDecided }: InboxPanelProps) {
  const [disabled, setDisabled] = useState(false);
  const disabledRef = useRef(false);
  const [items, setItems] = useState<InboxItem[]>([]);
  const [unreadCount, setUnreadCount] = useState(0);
  const [routinesList, setRoutinesList] = useState<Routine[]>([]);
  const [error, setError] = useState('');
  const [approvalsInProgress, setApprovalsInProgress] = useState<Record<string, boolean>>({});
  const fetchGen = useRef(0);

  // Create form state
  const [formOpen, setFormOpen] = useState(false);
  const [name, setName] = useState('');
  const [schedule, setSchedule] = useState('');
  const [prompt, setPrompt] = useState('');
  const [deliverTg, setDeliverTg] = useState(false);
  const [deliverFs, setDeliverFs] = useState(false);
  const [formBusy, setFormBusy] = useState(false);
  const [formError, setFormError] = useState('');

  const fetchInbox = useCallback(async () => {
    if (disabledRef.current || !token) return;
    const gen = ++fetchGen.current;
    try {
      const res = await fetch('/api/inbox?limit=50', {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.status === 409) {
        disabledRef.current = true;
        setDisabled(true);
        return;
      }
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.error || `Failed to load inbox (${res.status})`);
      }
      const data = await res.json();
      if (isStale(gen, fetchGen.current)) return;
      setItems(data.items || []);
      const count = Number(data.unread || 0);
      setUnreadCount(count);
      onUnreadChange?.(count);
    } catch (err) {
      if (isStale(gen, fetchGen.current) || disabledRef.current) return;
      setError(err instanceof Error ? err.message : 'Failed to fetch inbox');
    }
  }, [token, onUnreadChange]);

  const fetchRoutines = useCallback(async () => {
    if (disabledRef.current || !token) return;
    try {
      const res = await fetch('/api/routines', {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.status === 409) {
        disabledRef.current = true;
        setDisabled(true);
        return;
      }
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.error || `Failed to load routines (${res.status})`);
      }
      const data = await res.json();
      setRoutinesList(data.routines || []);
    } catch (err) {
      if (!disabledRef.current) {
        setError(err instanceof Error ? err.message : 'Failed to fetch routines');
      }
    }
  }, [token]);

  useEffect(() => {
    void fetchInbox();
    void fetchRoutines();
  }, [fetchInbox, fetchRoutines]);

  useEffect(() => {
    if (disabled || disabledRef.current) return;
    const timer = window.setInterval(() => {
      if (disabledRef.current) return;
      void fetchInbox();
      void fetchRoutines();
    }, 15_000);
    return () => window.clearInterval(timer);
  }, [fetchInbox, fetchRoutines, disabled]);

  const handleMarkRead = async (runId: number) => {
    try {
      await fetch(`/api/inbox/${runId}/read`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: '{}',
      });
      setItems((prev) =>
        prev.map((it) => (it.id === runId ? { ...it, read_at: new Date().toISOString() } : it))
      );
      setUnreadCount((c) => Math.max(0, c - 1));
      onUnreadChange?.(Math.max(0, unreadCount - 1));
    } catch {
      // ignore
    }
  };

  const handleMarkAllRead = async () => {
    try {
      await fetch('/api/inbox/read-all', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: '{}',
      });
      setItems((prev) =>
        prev.map((it) => ({ ...it, read_at: it.read_at || new Date().toISOString() }))
      );
      setUnreadCount(0);
      onUnreadChange?.(0);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to mark all read');
    }
  };

  const handleApprovalDecision = async (approvalId: string, approve: boolean) => {
    setApprovalsInProgress((prev) => ({ ...prev, [approvalId]: true }));
    try {
      const action = approve ? 'approve' : 'reject';
      const res = await fetch(`/api/approvals/${encodeURIComponent(approvalId)}/${action}`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: '{}',
      });
      if (!res.ok && res.status !== 404) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.error || `Approval failed (${res.status})`);
      }
      const status = !res.ok ? 'expired' : approve ? 'approved' : 'denied';
      // Drop an in-flight poll, then paint the decision before the refetch returns.
      fetchGen.current += 1;
      setItems((prev) => applyInboxDecision(prev, approvalId, status));
      await fetchInbox();
      onApprovalDecided?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Approval failed');
    } finally {
      setApprovalsInProgress((prev) => ({ ...prev, [approvalId]: false }));
    }
  };

  const handleToggleRoutine = async (r: Routine) => {
    const action = r.enabled ? 'pause' : 'resume';
    try {
      const res = await fetch(`/api/routines/${r.id}/${action}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: '{}',
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.error || `Failed to ${action} routine`);
      }
      await fetchRoutines();
    } catch (err) {
      setError(err instanceof Error ? err.message : `Failed to ${action} routine`);
    }
  };

  const handleRunNow = async (id: number) => {
    try {
      const res = await fetch(`/api/routines/${id}/run`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: '{}',
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.error || 'Failed to run routine');
      }
      await fetchInbox();
      await fetchRoutines();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to run routine');
    }
  };

  const handleDeleteRoutine = async (id: number) => {
    if (!window.confirm(`Delete routine #${id}?`)) return;
    try {
      const res = await fetch(`/api/routines/${id}`, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.error || 'Failed to delete routine');
      }
      await fetchRoutines();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete routine');
    }
  };

  const handleCreateRoutine = async (e: FormEvent) => {
    e.preventDefault();
    setFormBusy(true);
    setFormError('');
    const deliver = ['web'];
    if (deliverTg) deliver.push('telegram');
    if (deliverFs) deliver.push('feishu');

    try {
      const res = await fetch('/api/routines', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          name: name.trim(),
          schedule: schedule.trim(),
          prompt: prompt.trim(),
          deliver,
        }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.error || `Create routine failed (${res.status})`);
      }
      setName('');
      setSchedule('');
      setPrompt('');
      setDeliverTg(false);
      setDeliverFs(false);
      setFormOpen(false);
      await fetchRoutines();
    } catch (err) {
      setFormError(err instanceof Error ? err.message : 'Failed to create routine');
    } finally {
      setFormBusy(false);
    }
  };

  if (disabled) {
    return (
      <div className="stream-body" style={{ padding: '24px 20px' }}>
        <div className="job-notice" style={{ maxWidth: 640 }}>
          <strong>Routines are disabled</strong>
          <span>Set <code>CONVEYOR_ROUTINES_ENABLED=true</code> on the server to enable scheduled routines and inbox.</span>
        </div>
      </div>
    );
  }

  return (
    <div className="stream-body" style={{ display: 'flex', flexDirection: 'column', height: '100%', overflowY: 'auto', padding: '16px' }}>
      {error && (
        <div className="error-banner global" style={{ margin: '0 0 12px 0' }}>
          {error}
          <button type="button" onClick={() => setError('')}>×</button>
        </div>
      )}

      {/* Top section: Routines Manager */}
      <section style={{ marginBottom: 24, padding: 14, background: 'var(--panel-bg, #1a1a1a)', borderRadius: 8, border: '1px solid var(--border-color, #333)' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
          <h3 style={{ margin: 0, fontSize: '0.95rem', fontWeight: 600 }}>
            Scheduled Routines ({routinesList.length})
          </h3>
          <button
            type="button"
            className="action-btn"
            style={{ fontSize: '0.8rem', padding: '4px 8px' }}
            onClick={() => setFormOpen((o) => !o)}
          >
            {formOpen ? 'Cancel' : '＋ New Routine'}
          </button>
        </div>

        {formOpen && (
          <form onSubmit={handleCreateRoutine} style={{ marginBottom: 16, padding: 12, background: 'var(--input-bg, #222)', borderRadius: 6 }}>
            {formError && <div style={{ color: 'var(--color-danger, #ff4d4f)', fontSize: '0.85rem', marginBottom: 8 }}>{formError}</div>}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, marginBottom: 8 }}>
              <div>
                <label style={{ display: 'block', fontSize: '0.75rem', color: '#888', marginBottom: 2 }}>Routine Name</label>
                <input
                  type="text"
                  placeholder="Daily News Summary"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  style={{ width: '100%', padding: '6px 8px', borderRadius: 4, border: '1px solid #444', background: '#111', color: '#fff' }}
                  required
                />
              </div>
              <div>
                <label style={{ display: 'block', fontSize: '0.75rem', color: '#888', marginBottom: 2 }}>Cron (5-field)</label>
                <input
                  type="text"
                  placeholder="0 8 * * 1-5"
                  value={schedule}
                  onChange={(e) => setSchedule(e.target.value)}
                  style={{ width: '100%', padding: '6px 8px', borderRadius: 4, border: '1px solid #444', background: '#111', color: '#fff' }}
                  required
                />
              </div>
            </div>
            <div style={{ marginBottom: 8 }}>
              <label style={{ display: 'block', fontSize: '0.75rem', color: '#888', marginBottom: 2 }}>Prompt (instructions for chat tier + tools)</label>
              <textarea
                placeholder="Check recent issues on GitHub and summarize today's priorities."
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                rows={3}
                style={{ width: '100%', padding: '6px 8px', borderRadius: 4, border: '1px solid #444', background: '#111', color: '#fff', resize: 'vertical' }}
                required
              />
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 16, marginBottom: 12 }}>
              <span style={{ fontSize: '0.75rem', color: '#888' }}>Delivery:</span>
              <label style={{ fontSize: '0.8rem', display: 'flex', alignItems: 'center', gap: 4 }}>
                <input type="checkbox" checked disabled /> Web Inbox
              </label>
              <label style={{ fontSize: '0.8rem', display: 'flex', alignItems: 'center', gap: 4 }}>
                <input type="checkbox" checked={deliverTg} onChange={(e) => setDeliverTg(e.target.checked)} /> Telegram
              </label>
              <label style={{ fontSize: '0.8rem', display: 'flex', alignItems: 'center', gap: 4 }}>
                <input type="checkbox" checked={deliverFs} onChange={(e) => setDeliverFs(e.target.checked)} /> Feishu
              </label>
            </div>
            <button type="submit" disabled={formBusy} className="action-btn" style={{ padding: '6px 14px' }}>
              {formBusy ? 'Creating…' : 'Create Routine'}
            </button>
          </form>
        )}

        {routinesList.length === 0 ? (
          <p style={{ margin: 0, color: '#888', fontSize: '0.85rem' }}>No routines created yet.</p>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {routinesList.map((r) => (
              <div
                key={r.id}
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                  padding: '8px 12px',
                  background: 'var(--item-bg, #222)',
                  borderRadius: 6,
                  border: '1px solid #333',
                }}
              >
                <div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span style={{ fontSize: '0.9rem' }}>{r.enabled ? '🟢' : '⏸️'}</span>
                    <strong>{r.name}</strong>
                    <code style={{ fontSize: '0.8rem', background: '#111', padding: '2px 6px', borderRadius: 4 }}>
                      {r.schedule_cron || r.schedule}
                    </code>
                    {r.consecutive_failures ? (
                      <span style={{ fontSize: '0.75rem', color: '#ff7875' }}>
                        ({r.consecutive_failures} failure{r.consecutive_failures > 1 ? 's' : ''})
                      </span>
                    ) : null}
                  </div>
                  <div style={{ fontSize: '0.75rem', color: '#888', marginTop: 4 }}>
                    Next run: {r.enabled ? formatLocalTime(r.next_run_at) : 'paused'}
                    {r.last_run_at ? ` · Last run: ${formatLocalTime(r.last_run_at)} (${r.last_run_status || 'unknown'})` : ''}
                  </div>
                </div>
                <div style={{ display: 'flex', gap: 6 }}>
                  <button
                    type="button"
                    className="action-btn"
                    style={{ fontSize: '0.75rem', padding: '4px 8px' }}
                    onClick={() => void handleRunNow(r.id)}
                    title="Run routine now"
                  >
                    Run now
                  </button>
                  <button
                    type="button"
                    className="action-btn"
                    style={{ fontSize: '0.75rem', padding: '4px 8px' }}
                    onClick={() => void handleToggleRoutine(r)}
                  >
                    {r.enabled ? 'Pause' : 'Resume'}
                  </button>
                  <button
                    type="button"
                    className="action-btn"
                    style={{ fontSize: '0.75rem', padding: '4px 8px', color: '#ff7875' }}
                    onClick={() => void handleDeleteRoutine(r.id)}
                  >
                    Delete
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* Bottom section: Inbox Runs */}
      <section style={{ flex: 1 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
          <h3 style={{ margin: 0, fontSize: '0.95rem', fontWeight: 600 }}>
            Inbox {unreadCount > 0 ? `(${unreadCount} unread)` : ''}
          </h3>
          {unreadCount > 0 && (
            <button
              type="button"
              className="action-btn"
              style={{ fontSize: '0.8rem', padding: '4px 8px' }}
              onClick={() => void handleMarkAllRead()}
            >
              Mark all read
            </button>
          )}
        </div>

        {items.length === 0 ? (
          <p style={{ color: '#888', fontSize: '0.85rem' }}>No inbox items yet.</p>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            {items.map((item) => {
              const isUnread = !item.read_at;
              const appr = item.approval;
              const inProgress = appr ? Boolean(approvalsInProgress[appr.id]) : false;

              let statusBadgeClass = 'completed';
              let statusLabel = item.status;
              if (item.status === 'approval_pending' && appr && appr.status !== 'pending') {
                statusLabel = `approval ${appr.status}`;
                statusBadgeClass = appr.status === 'approved' ? 'completed' : 'interrupted';
              } else if (item.status === 'approval_pending') statusBadgeClass = 'queued';
              else if (item.status === 'error') statusBadgeClass = 'failed';
              else if (['escalate', 'denied', 'expired', 'cancelled'].includes(item.status)) statusBadgeClass = 'interrupted';

              return (
                <article
                  key={item.id}
                  onClick={() => {
                    if (isUnread) void handleMarkRead(item.id);
                  }}
                  style={{
                    padding: 14,
                    background: isUnread ? 'var(--unread-bg, #22252c)' : 'var(--panel-bg, #1a1a1a)',
                    borderLeft: isUnread ? '4px solid #1677ff' : '4px solid transparent',
                    borderTop: '1px solid #333',
                    borderRight: '1px solid #333',
                    borderBottom: '1px solid #333',
                    borderRadius: 6,
                    cursor: isUnread ? 'pointer' : 'default',
                  }}
                >
                  <header style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      <strong>{item.routine_name}</strong>
                      <span className={`status-badge ${statusBadgeClass}`} style={{ fontSize: '0.75rem', padding: '2px 6px' }}>
                        {statusLabel}
                      </span>
                      {isUnread && (
                        <span style={{ fontSize: '0.75rem', color: '#1677ff', fontWeight: 600 }}>● New</span>
                      )}
                    </div>
                    <time style={{ fontSize: '0.75rem', color: '#888' }} dateTime={item.started_at}>
                      {formatLocalTime(item.started_at)}
                    </time>
                  </header>

                  <div style={{ fontSize: '0.88rem', lineHeight: 1.5, marginBottom: appr ? 10 : 0 }}>
                    <FormattedText content={item.output} />
                  </div>

                  {appr && (
                    <div style={{ marginTop: 10, paddingTop: 10, borderTop: '1px dashed #444' }}>
                      {appr.status === 'pending' ? (
                        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                          <span style={{ fontSize: '0.8rem', color: '#faad14' }}>
                            ⚠️ Approval Required
                            {appr.expires_at ? ` (expires ${formatLocalTime(appr.expires_at)})` : ''}
                          </span>
                          <button
                            type="button"
                            className="action-btn"
                            style={{ fontSize: '0.8rem', padding: '4px 10px', background: '#389e0d', color: '#fff' }}
                            disabled={inProgress}
                            onClick={(e) => {
                              e.stopPropagation();
                              void handleApprovalDecision(appr.id, true);
                            }}
                          >
                            {inProgress ? 'Executing…' : 'Approve'}
                          </button>
                          <button
                            type="button"
                            className="action-btn"
                            style={{ fontSize: '0.8rem', padding: '4px 10px', background: '#cf1322', color: '#fff' }}
                            disabled={inProgress}
                            onClick={(e) => {
                              e.stopPropagation();
                              void handleApprovalDecision(appr.id, false);
                            }}
                          >
                            {inProgress ? 'Rejecting…' : 'Deny'}
                          </button>
                        </div>
                      ) : (
                        <div style={{ fontSize: '0.8rem', color: '#aaa' }}>
                          {appr.status === 'approved'
                            ? '✅ Approved'
                            : appr.status === 'denied'
                            ? '❌ Denied'
                            : appr.status === 'cancelled'
                            ? '🚫 Cancelled (routine deleted)'
                            : '⌛ Expired — not executed'}
                        </div>
                      )}
                    </div>
                  )}
                </article>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}
