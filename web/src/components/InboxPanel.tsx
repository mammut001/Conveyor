import React, { FormEvent, useCallback, useEffect, useRef, useState } from 'react';
import { applyInboxDecision, isStale } from '../approvalFreshness';
import { FormattedText } from './FormattedText';

export type RoutineHook = {
  hook_id: string;
  created_at: string;
  last_fired_at?: string | null;
  fire_count: number;
};

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
  hook?: RoutineHook | null;
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
  trigger?: string;
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
  webhooksEnabled?: boolean;
};

type CreatedHookInfo = {
  routineId: number;
  routineName: string;
  hook_id: string;
  secret: string;
  path: string;
};

type ConfirmHookAction = {
  routineId: number;
  action: 'rotate' | 'delete';
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

export function InboxPanel({ token, onUnreadChange, onApprovalDecided, webhooksEnabled }: InboxPanelProps) {
  const [disabled, setDisabled] = useState(false);
  const disabledRef = useRef(false);
  const [items, setItems] = useState<InboxItem[]>([]);
  const [unreadCount, setUnreadCount] = useState(0);
  const [routinesList, setRoutinesList] = useState<Routine[]>([]);
  const [error, setError] = useState('');
  const [approvalsInProgress, setApprovalsInProgress] = useState<Record<string, boolean>>({});
  const fetchGen = useRef(0);

  // Webhook state
  const [confirmHookAction, setConfirmHookAction] = useState<ConfirmHookAction | null>(null);
  const [createdHookInfo, setCreatedHookInfo] = useState<CreatedHookInfo | null>(null);
  const [hookBusyId, setHookBusyId] = useState<number | null>(null);
  const [copiedSecret, setCopiedSecret] = useState(false);
  const [copiedCurl, setCopiedCurl] = useState(false);

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

  const handleCreateOrRotateHook = async (routine: Routine, action: 'create' | 'rotate') => {
    setHookBusyId(routine.id);
    setError('');
    try {
      const res = await fetch(`/api/routines/${routine.id}/hook`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: '{}',
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.error || `Failed to ${action} webhook (${res.status})`);
      }
      const data = await res.json();
      setCreatedHookInfo({
        routineId: routine.id,
        routineName: routine.name,
        hook_id: data.hook_id,
        secret: data.secret,
        path: data.path,
      });
      setConfirmHookAction(null);
      setCopiedSecret(false);
      setCopiedCurl(false);
      await fetchRoutines();
    } catch (err) {
      setError(err instanceof Error ? err.message : `Failed to ${action} webhook`);
    } finally {
      setHookBusyId(null);
    }
  };

  const handleDeleteHook = async (routineId: number) => {
    setHookBusyId(routineId);
    setError('');
    try {
      const res = await fetch(`/api/routines/${routineId}/hook`, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok && res.status !== 404) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.error || `Failed to delete webhook (${res.status})`);
      }
      if (createdHookInfo?.routineId === routineId) {
        setCreatedHookInfo(null);
      }
      setConfirmHookAction(null);
      await fetchRoutines();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete webhook');
    } finally {
      setHookBusyId(null);
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
      <section style={{ marginBottom: 24, padding: 14, background: 'var(--panel, #fff)', borderRadius: 8, border: '1px solid var(--line)' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
          <h3 style={{ margin: 0, fontSize: '0.95rem', fontWeight: 600, color: 'var(--text)' }}>
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

        {createdHookInfo && (
          <div
            style={{
              marginBottom: 16,
              padding: 14,
              background: 'var(--panel-2, #f8fafc)',
              border: '1px solid #bfdbfe',
              borderRadius: 8,
            }}
          >
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 8 }}>
              <div>
                <strong style={{ fontSize: '0.9rem', color: 'var(--text)' }}>
                  Webhook credentials for &ldquo;{createdHookInfo.routineName}&rdquo;
                </strong>
                <p style={{ margin: '4px 0 0', fontSize: '0.8rem', color: 'var(--yellow)', fontWeight: 600 }}>
                  ⚠️ The secret is shown only now.
                </p>
              </div>
              <button
                type="button"
                className="action-btn"
                style={{ fontSize: '0.75rem', padding: '3px 8px' }}
                onClick={() => setCreatedHookInfo(null)}
              >
                Dismiss
              </button>
            </div>

            <div style={{ display: 'grid', gap: 8, fontSize: '0.82rem', marginTop: 10 }}>
              <div>
                <span style={{ color: 'var(--muted)', display: 'block', fontSize: '0.75rem', marginBottom: 2 }}>Webhook URL</span>
                <code style={{ display: 'block', padding: '6px 8px', background: '#fff', border: '1px solid var(--line)', borderRadius: 4, overflowX: 'auto', color: 'var(--text)' }}>
                  {typeof window !== 'undefined' ? `${window.location.origin}${createdHookInfo.path}` : createdHookInfo.path}
                </code>
              </div>

              <div>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 2 }}>
                  <span style={{ color: 'var(--muted)', fontSize: '0.75rem' }}>Secret</span>
                  <button
                    type="button"
                    className="action-btn"
                    style={{ fontSize: '0.7rem', padding: '2px 6px' }}
                    onClick={() => {
                      void navigator.clipboard.writeText(createdHookInfo.secret);
                      setCopiedSecret(true);
                      setTimeout(() => setCopiedSecret(false), 2000);
                    }}
                  >
                    {copiedSecret ? 'Copied!' : 'Copy Secret'}
                  </button>
                </div>
                <code style={{ display: 'block', padding: '6px 8px', background: '#fff', border: '1px solid var(--line)', borderRadius: 4, overflowX: 'auto', wordBreak: 'break-all', color: 'var(--text)' }}>
                  {createdHookInfo.secret}
                </code>
              </div>

              <div>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 2 }}>
                  <span style={{ color: 'var(--muted)', fontSize: '0.75rem' }}>Ready-to-copy curl example</span>
                  <button
                    type="button"
                    className="action-btn"
                    style={{ fontSize: '0.7rem', padding: '2px 6px' }}
                    onClick={() => {
                      const fullUrl = typeof window !== 'undefined' ? `${window.location.origin}${createdHookInfo.path}` : createdHookInfo.path;
                      const snippet = `BODY='{"event":"ping"}'\nSECRET='${createdHookInfo.secret}'\nSIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $NF}')\ncurl -X POST "${fullUrl}" \\\n  -H "Content-Type: application/json" \\\n  -H "X-Conveyor-Signature: sha256=$SIG" \\\n  -d "$BODY"`;
                      void navigator.clipboard.writeText(snippet);
                      setCopiedCurl(true);
                      setTimeout(() => setCopiedCurl(false), 2000);
                    }}
                  >
                    {copiedCurl ? 'Copied!' : 'Copy curl'}
                  </button>
                </div>
                <pre style={{ margin: 0, padding: '8px 10px', background: '#fff', border: '1px solid var(--line)', borderRadius: 4, fontSize: '0.75rem', overflowX: 'auto', whiteSpace: 'pre-wrap', lineHeight: 1.4, color: 'var(--text)' }}>
{`BODY='{"event":"ping"}'
SECRET='${createdHookInfo.secret}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $NF}')
curl -X POST "${typeof window !== 'undefined' ? window.location.origin : ''}${createdHookInfo.path}" \\
  -H "Content-Type: application/json" \\
  -H "X-Conveyor-Signature: sha256=$SIG" \\
  -d "$BODY"`}
                </pre>
              </div>
            </div>
          </div>
        )}

        {formOpen && (
          <form onSubmit={handleCreateRoutine} style={{ marginBottom: 16, padding: 12, background: 'var(--panel-2, #f8fafc)', borderRadius: 6, border: '1px solid var(--line)' }}>
            {formError && <div style={{ color: 'var(--red)', fontSize: '0.85rem', marginBottom: 8 }}>{formError}</div>}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, marginBottom: 8 }}>
              <div>
                <label style={{ display: 'block', fontSize: '0.75rem', color: 'var(--muted)', marginBottom: 2 }}>Routine Name</label>
                <input
                  type="text"
                  placeholder="Daily News Summary"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  style={{ width: '100%', padding: '6px 8px', borderRadius: 4, border: '1px solid var(--line)', background: '#fff', color: 'var(--text)' }}
                  required
                />
              </div>
              <div>
                <label style={{ display: 'block', fontSize: '0.75rem', color: 'var(--muted)', marginBottom: 2 }}>Cron (5-field)</label>
                <input
                  type="text"
                  placeholder="0 8 * * 1-5"
                  value={schedule}
                  onChange={(e) => setSchedule(e.target.value)}
                  style={{ width: '100%', padding: '6px 8px', borderRadius: 4, border: '1px solid var(--line)', background: '#fff', color: 'var(--text)' }}
                  required
                />
              </div>
            </div>
            <div style={{ marginBottom: 8 }}>
              <label style={{ display: 'block', fontSize: '0.75rem', color: 'var(--muted)', marginBottom: 2 }}>Prompt (instructions for chat tier + tools)</label>
              <textarea
                placeholder="Check recent issues on GitHub and summarize today's priorities."
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                rows={3}
                style={{ width: '100%', padding: '6px 8px', borderRadius: 4, border: '1px solid var(--line)', background: '#fff', color: 'var(--text)', resize: 'vertical' }}
                required
              />
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 16, marginBottom: 12 }}>
              <span style={{ fontSize: '0.75rem', color: 'var(--muted)' }}>Delivery:</span>
              <label style={{ fontSize: '0.8rem', display: 'flex', alignItems: 'center', gap: 4, color: 'var(--text)' }}>
                <input type="checkbox" checked disabled /> Web Inbox
              </label>
              <label style={{ fontSize: '0.8rem', display: 'flex', alignItems: 'center', gap: 4, color: 'var(--text)' }}>
                <input type="checkbox" checked={deliverTg} onChange={(e) => setDeliverTg(e.target.checked)} /> Telegram
              </label>
              <label style={{ fontSize: '0.8rem', display: 'flex', alignItems: 'center', gap: 4, color: 'var(--text)' }}>
                <input type="checkbox" checked={deliverFs} onChange={(e) => setDeliverFs(e.target.checked)} /> Feishu
              </label>
            </div>
            <button type="submit" disabled={formBusy} className="action-btn" style={{ padding: '6px 14px' }}>
              {formBusy ? 'Creating…' : 'Create Routine'}
            </button>
          </form>
        )}

        {routinesList.length === 0 ? (
          <p style={{ margin: 0, color: 'var(--muted)', fontSize: '0.85rem' }}>No routines created yet.</p>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {routinesList.map((r) => (
              <div
                key={r.id}
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                  padding: '10px 12px',
                  background: 'var(--panel-2, #f8fafc)',
                  borderRadius: 6,
                  border: '1px solid var(--line)',
                  flexWrap: 'wrap',
                  gap: 8,
                }}
              >
                <div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                    <span style={{ fontSize: '0.9rem' }}>{r.enabled ? '🟢' : '⏸️'}</span>
                    <strong style={{ color: 'var(--text)' }}>{r.name}</strong>
                    <code style={{ fontSize: '0.8rem', background: '#fff', border: '1px solid var(--line)', padding: '2px 6px', borderRadius: 4, color: 'var(--text)' }}>
                      {r.schedule_cron || r.schedule}
                    </code>
                    {r.consecutive_failures ? (
                      <span style={{ fontSize: '0.75rem', color: 'var(--red)' }}>
                        ({r.consecutive_failures} failure{r.consecutive_failures > 1 ? 's' : ''})
                      </span>
                    ) : null}
                  </div>
                  <div style={{ fontSize: '0.75rem', color: 'var(--muted)', marginTop: 4 }}>
                    Next run: {r.enabled ? formatLocalTime(r.next_run_at) : 'paused'}
                    {r.last_run_at ? ` · Last run: ${formatLocalTime(r.last_run_at)} (${r.last_run_status || 'unknown'})` : ''}
                    {webhooksEnabled && r.hook && (
                      <span>
                        {' · Webhook: '}
                        {r.hook.fire_count} {r.hook.fire_count === 1 ? 'run' : 'runs'}
                        {r.hook.last_fired_at ? ` · Last fired: ${formatLocalTime(r.hook.last_fired_at)}` : ' (never fired)'}
                      </span>
                    )}
                  </div>
                </div>
                <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
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

                  {webhooksEnabled && (
                    <>
                      {!r.hook ? (
                        <button
                          type="button"
                          className="action-btn"
                          style={{ fontSize: '0.75rem', padding: '4px 8px' }}
                          disabled={hookBusyId === r.id}
                          onClick={() => void handleCreateOrRotateHook(r, 'create')}
                        >
                          {hookBusyId === r.id ? 'Creating…' : 'Create webhook'}
                        </button>
                      ) : confirmHookAction?.routineId === r.id && confirmHookAction.action === 'rotate' ? (
                        <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
                          <span className="memory-confirm-warning" style={{ fontSize: '0.75rem' }}>Rotate?</span>
                          <button
                            type="button"
                            className="action-btn memory-btn-danger"
                            style={{ fontSize: '0.75rem', padding: '3px 8px' }}
                            disabled={hookBusyId === r.id}
                            onClick={() => void handleCreateOrRotateHook(r, 'rotate')}
                          >
                            {hookBusyId === r.id ? 'Rotating…' : 'Confirm'}
                          </button>
                          <button
                            type="button"
                            className="action-btn"
                            style={{ fontSize: '0.75rem', padding: '3px 8px' }}
                            disabled={hookBusyId === r.id}
                            onClick={() => setConfirmHookAction(null)}
                          >
                            Cancel
                          </button>
                        </div>
                      ) : confirmHookAction?.routineId === r.id && confirmHookAction.action === 'delete' ? (
                        <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
                          <span className="memory-confirm-warning" style={{ fontSize: '0.75rem' }}>Remove?</span>
                          <button
                            type="button"
                            className="action-btn memory-btn-danger"
                            style={{ fontSize: '0.75rem', padding: '3px 8px' }}
                            disabled={hookBusyId === r.id}
                            onClick={() => void handleDeleteHook(r.id)}
                          >
                            {hookBusyId === r.id ? 'Removing…' : 'Confirm'}
                          </button>
                          <button
                            type="button"
                            className="action-btn"
                            style={{ fontSize: '0.75rem', padding: '3px 8px' }}
                            disabled={hookBusyId === r.id}
                            onClick={() => setConfirmHookAction(null)}
                          >
                            Cancel
                          </button>
                        </div>
                      ) : (
                        <>
                          <button
                            type="button"
                            className="action-btn"
                            style={{ fontSize: '0.75rem', padding: '4px 8px' }}
                            disabled={hookBusyId === r.id}
                            onClick={() => setConfirmHookAction({ routineId: r.id, action: 'rotate' })}
                          >
                            Rotate
                          </button>
                          <button
                            type="button"
                            className="action-btn"
                            style={{ fontSize: '0.75rem', padding: '4px 8px', color: 'var(--red)' }}
                            disabled={hookBusyId === r.id}
                            onClick={() => setConfirmHookAction({ routineId: r.id, action: 'delete' })}
                          >
                            Remove
                          </button>
                        </>
                      )}
                    </>
                  )}

                  <button
                    type="button"
                    className="action-btn"
                    style={{ fontSize: '0.75rem', padding: '4px 8px', color: 'var(--red)' }}
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
          <h3 style={{ margin: 0, fontSize: '0.95rem', fontWeight: 600, color: 'var(--text)' }}>
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
          <p style={{ color: 'var(--muted)', fontSize: '0.85rem' }}>No inbox items yet.</p>
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
                    background: isUnread ? '#f0f7ff' : 'var(--panel, #fff)',
                    borderLeft: isUnread ? '4px solid var(--accent)' : '4px solid transparent',
                    borderTop: '1px solid var(--line)',
                    borderRight: '1px solid var(--line)',
                    borderBottom: '1px solid var(--line)',
                    borderRadius: 6,
                    cursor: isUnread ? 'pointer' : 'default',
                  }}
                >
                  <header style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                      <strong style={{ color: 'var(--text)' }}>{item.routine_name}</strong>
                      <span className={`status-badge ${statusBadgeClass}`} style={{ fontSize: '0.75rem', padding: '2px 6px' }}>
                        {statusLabel}
                      </span>
                      {item.trigger === 'webhook' && (
                        <span className="status-badge webhook" style={{ fontSize: '0.75rem', padding: '2px 8px' }}>
                          via webhook
                        </span>
                      )}
                      {isUnread && (
                        <span style={{ fontSize: '0.75rem', color: 'var(--accent)', fontWeight: 600 }}>● New</span>
                      )}
                    </div>
                    <time style={{ fontSize: '0.75rem', color: 'var(--muted)' }} dateTime={item.started_at}>
                      {formatLocalTime(item.started_at)}
                    </time>
                  </header>

                  <div style={{ fontSize: '0.88rem', lineHeight: 1.5, marginBottom: appr ? 10 : 0, color: 'var(--text)' }}>
                    <FormattedText content={item.output} />
                  </div>

                  {appr && (
                    <div style={{ marginTop: 10, paddingTop: 10, borderTop: '1px dashed var(--line)' }}>
                      {appr.status === 'pending' ? (
                        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                          <span style={{ fontSize: '0.8rem', color: 'var(--yellow)', fontWeight: 600 }}>
                            ⚠️ Approval Required
                            {appr.expires_at ? ` (expires ${formatLocalTime(appr.expires_at)})` : ''}
                          </span>
                          <button
                            type="button"
                            className="action-btn"
                            style={{ fontSize: '0.8rem', padding: '4px 10px', background: '#389e0d', color: '#fff', border: '1px solid #389e0d' }}
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
                            className="action-btn memory-btn-danger"
                            style={{ fontSize: '0.8rem', padding: '4px 10px' }}
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
                        <div style={{ fontSize: '0.8rem', color: 'var(--muted)' }}>
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

