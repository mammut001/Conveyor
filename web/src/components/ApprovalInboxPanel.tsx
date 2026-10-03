import React, { useCallback, useEffect, useRef, useState } from 'react';
import { isStale } from '../approvalFreshness';

export type ApprovalInboxItem = {
  id: string;
  kind: 'tool' | 'job';
  source: 'chat' | 'routine' | 'webhook' | 'job';
  action?: string; // apply | discard for job
  job_id?: string;
  tool_name?: string;
  summary?: string;
  danger?: string; // write | write_safe
  arg?: string;
  draft?: Record<string, string> | null;
  editable: boolean;
  redacted?: boolean;
  created_at: string | number;
  expires_at?: string | number;
  session_id?: string;
  routine_id?: number | null;
  routine_name?: string | null;
};

export type ApprovalInboxPanelProps = {
  token: string;
  onApprovalDecided?: () => void;
  onPendingCountChange?: (count: number) => void;
};

function formatLocalTime(val?: string | number | null) {
  if (!val) return '—';
  const d = typeof val === 'number' ? new Date(val > 1e11 ? val : val * 1000) : new Date(val);
  return Number.isNaN(d.valueOf())
    ? String(val)
    : d.toLocaleString([], {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      });
}

function truncateStr(s: string, maxLen = 30): string {
  if (!s) return '""';
  if (s.length <= maxLen) return JSON.stringify(s);
  return JSON.stringify(s.slice(0, maxLen) + '…');
}

function getFieldValidationHint(field: string, value: string): string | null {
  if (typeof value !== 'string') return 'Must be text';
  const SINGLE_LINE = ['to', 'subject', 'number', 'title', 'cron', 'name'];
  if (SINGLE_LINE.includes(field)) {
    if (value.includes('|')) return 'Cannot contain pipes (|)';
    if (value.includes('\n') || value.includes('\r')) return 'Cannot contain newlines';
  }
  if (field === 'prompt' && value.includes('|')) {
    return 'Prompt cannot contain pipes (|)';
  }
  if (field === 'number') {
    if (!value.trim() || !/^\d+$/.test(value.trim())) return 'Digits only';
  }
  if (field === 'to') {
    const parts = value.split(',').map((s) => s.trim());
    if (!parts.length || parts.some((p) => !p)) return 'Comma-separated email addresses required';
    const emailPattern = /^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$/;
    for (const p of parts) {
      const clean = p.includes('<') && p.includes('>') ? p.replace(/^.*<([^>]+)>.*$/, '$1') : p;
      if (!emailPattern.test(clean)) return `Invalid email: ${p}`;
    }
  }
  const CAPS: Record<string, number> = {
    to: 320,
    subject: 200,
    title: 200,
    name: 80,
    body: 20000,
    text: 4000,
    prompt: 4000,
  };
  const cap = CAPS[field];
  if (cap && value.length > cap) {
    return `Max ${cap} characters (${value.length})`;
  }
  if (['to', 'subject', 'title', 'text', 'cron'].includes(field) && !value.trim()) {
    return 'Cannot be empty';
  }
  if (field === 'prompt' && !value.trim()) {
    return 'Cannot be empty';
  }
  return null;
}

function getDraftValidationError(draft: Record<string, string>): string | null {
  for (const [field, value] of Object.entries(draft)) {
    const hint = getFieldValidationHint(field, value);
    if (hint) return `${field}: ${hint}`;
  }
  return null;
}

export function ApprovalInboxPanel({
  token,
  onApprovalDecided,
  onPendingCountChange,
}: ApprovalInboxPanelProps) {
  const [items, setItems] = useState<ApprovalInboxItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [globalError, setGlobalError] = useState('');
  const [busyMap, setBusyMap] = useState<Record<string, boolean>>({});
  const [inlineErrors, setInlineErrors] = useState<Record<string, string>>({});

  // Editing state
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState<Record<string, string>>({});
  const [editOriginalDraft, setEditOriginalDraft] = useState<Record<string, string>>({});
  const [editInitialArg, setEditInitialArg] = useState('');
  const [confirmApproveEditedId, setConfirmApproveEditedId] = useState<string | null>(null);

  // Reject confirmation state
  const [confirmRejectId, setConfirmRejectId] = useState<string | null>(null);

  const fetchGen = useRef(0);
  const itemsRef = useRef<ApprovalInboxItem[]>([]);
  useEffect(() => {
    itemsRef.current = items;
  }, [items]);

  // Remove a decided item and update the tab badge immediately (not on the next poll).
  const dropItem = useCallback(
    (id: string) => {
      const next = itemsRef.current.filter((it) => it.id !== id);
      itemsRef.current = next;
      setItems(next);
      onPendingCountChange?.(next.length);
    },
    [onPendingCountChange],
  );

  const fetchItems = useCallback(async () => {
    const gen = ++fetchGen.current;
    try {
      const res = await fetch('/api/approval-inbox', {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (isStale(gen, fetchGen.current)) return;
      if (!res.ok) {
        if (res.status === 409) {
          setGlobalError('Approval inbox is disabled in settings.');
          setItems([]);
          onPendingCountChange?.(0);
          setLoading(false);
          return;
        }
        throw new Error(`Failed to load approval inbox (HTTP ${res.status})`);
      }
      const data = await res.json();
      if (isStale(gen, fetchGen.current)) return;
      const fetchedItems: ApprovalInboxItem[] = data.items || [];
      setItems(fetchedItems);
      onPendingCountChange?.(data.counts?.total ?? fetchedItems.length);
      setGlobalError('');
      setLoading(false);
    } catch (err) {
      if (isStale(gen, fetchGen.current)) return;
      setGlobalError(err instanceof Error ? err.message : 'Error fetching approvals');
      setLoading(false);
    }
  }, [token, onPendingCountChange]);

  useEffect(() => {
    void fetchItems();
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') {
        void fetchItems();
      }
    }, 5_000);
    return () => window.clearInterval(timer);
  }, [fetchItems]);

  const handleApproveAsIs = async (item: ApprovalInboxItem) => {
    setBusyMap((prev) => ({ ...prev, [item.id]: true }));
    setInlineErrors((prev) => ({ ...prev, [item.id]: '' }));
    try {
      const bodyPayload = item.kind === 'tool' ? { expected_arg: item.arg } : {};
      const res = await fetch(`/api/approval-inbox/${encodeURIComponent(item.id)}/approve`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify(bodyPayload),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.error || `HTTP ${res.status}`);
      }
      fetchGen.current++;
      dropItem(item.id);
      onApprovalDecided?.();
      void fetchItems();
    } catch (err) {
      setInlineErrors((prev) => ({
        ...prev,
        [item.id]: err instanceof Error ? err.message : 'Approval failed',
      }));
    } finally {
      setBusyMap((prev) => ({ ...prev, [item.id]: false }));
    }
  };

  const startEdit = (item: ApprovalInboxItem) => {
    setEditingId(item.id);
    const draftCopy = { ...(item.draft || {}) };
    setEditDraft(draftCopy);
    setEditOriginalDraft(draftCopy);
    setEditInitialArg(item.arg || '');
    setConfirmApproveEditedId(null);
    setInlineErrors((prev) => ({ ...prev, [item.id]: '' }));
  };

  const cancelEdit = () => {
    setEditingId(null);
    setEditDraft({});
    setEditOriginalDraft({});
    setEditInitialArg('');
    setConfirmApproveEditedId(null);
  };

  const handleApproveEdited = async (item: ApprovalInboxItem) => {
    const valErr = getDraftValidationError(editDraft);
    if (valErr) {
      setInlineErrors((prev) => ({ ...prev, [item.id]: valErr }));
      return;
    }
    setBusyMap((prev) => ({ ...prev, [item.id]: true }));
    setInlineErrors((prev) => ({ ...prev, [item.id]: '' }));
    try {
      const res = await fetch(`/api/approval-inbox/${encodeURIComponent(item.id)}/approve`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          draft: editDraft,
          expected_arg: editInitialArg,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.error || `HTTP ${res.status}`);
      }
      fetchGen.current++;
      dropItem(item.id);
      cancelEdit();
      onApprovalDecided?.();
      void fetchItems();
    } catch (err) {
      setInlineErrors((prev) => ({
        ...prev,
        [item.id]: err instanceof Error ? err.message : 'Approval failed',
      }));
    } finally {
      setBusyMap((prev) => ({ ...prev, [item.id]: false }));
    }
  };

  const handleReject = async (item: ApprovalInboxItem) => {
    setBusyMap((prev) => ({ ...prev, [item.id]: true }));
    setInlineErrors((prev) => ({ ...prev, [item.id]: '' }));
    try {
      const res = await fetch(`/api/approval-inbox/${encodeURIComponent(item.id)}/reject`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: '{}',
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.error || `HTTP ${res.status}`);
      }
      fetchGen.current++;
      dropItem(item.id);
      setConfirmRejectId(null);
      onApprovalDecided?.();
      void fetchItems();
    } catch (err) {
      setInlineErrors((prev) => ({
        ...prev,
        [item.id]: err instanceof Error ? err.message : 'Rejection failed',
      }));
    } finally {
      setBusyMap((prev) => ({ ...prev, [item.id]: false }));
    }
  };

  const getChangedFields = () => {
    const changes: { field: string; oldVal: string; newVal: string }[] = [];
    for (const [key, newVal] of Object.entries(editDraft)) {
      const oldVal = editOriginalDraft[key] ?? '';
      if (oldVal !== newVal) {
        changes.push({ field: key, oldVal, newVal });
      }
    }
    return changes;
  };

  const sourceBadgeStyle = (source: ApprovalInboxItem['source']) => {
    switch (source) {
      case 'webhook':
        return { background: '#eff6ff', color: '#1d4ed8', border: '1px solid #bfdbfe' };
      case 'routine':
        return { background: '#f5f3ff', color: '#6d28d9', border: '1px solid #ddd6fe' };
      case 'job':
        return { background: '#fffbeb', color: '#b45309', border: '1px solid #fde68a' };
      case 'chat':
      default:
        return { background: '#f0fdf4', color: '#15803d', border: '1px solid #bbf7d0' };
    }
  };

  if (loading && items.length === 0) {
    return (
      <div style={{ padding: 24, textAlign: 'center', color: 'var(--muted)' }}>
        Loading approvals…
      </div>
    );
  }

  return (
    <div style={{ padding: '16px 20px', display: 'flex', flexDirection: 'column', gap: 14 }}>
      {globalError && <div className="error-banner">{globalError}</div>}

      {items.length === 0 && !globalError && (
        <div className="empty" style={{ padding: '36px 12px', textAlign: 'center' }}>
          No pending approvals at this time.
        </div>
      )}

      {items.map((item) => {
        const isEditing = editingId === item.id;
        const isBusy = Boolean(busyMap[item.id]);
        const changedFields = isEditing ? getChangedFields() : [];
        const draftError = isEditing ? getDraftValidationError(editDraft) : null;
        const inlineErr = inlineErrors[item.id];

        return (
          <article
            key={item.id}
            className="memory-card"
            style={{
              padding: 16,
              display: 'flex',
              flexDirection: 'column',
              gap: 12,
              borderColor: item.source === 'webhook' ? '#93c5fd' : undefined,
            }}
          >
            {/* Header row */}
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: 8 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                <span
                  style={{
                    padding: '2px 8px',
                    borderRadius: 4,
                    fontSize: '0.75rem',
                    fontWeight: 600,
                    textTransform: 'uppercase',
                    ...sourceBadgeStyle(item.source),
                  }}
                >
                  {item.source}
                </span>

                <strong style={{ fontSize: '0.95rem' }}>
                  {item.kind === 'tool' ? item.tool_name : `Job ${item.action || 'Approval'}`}
                </strong>

                {item.summary && (
                  <span className="memory-secondary" style={{ fontSize: '0.82rem' }}>
                    · {item.summary}
                  </span>
                )}

                {item.danger && (
                  <span
                    style={{
                      fontSize: '0.72rem',
                      padding: '1px 5px',
                      borderRadius: 3,
                      background: item.danger === 'write' ? '#fef2f2' : '#f8fafc',
                      color: item.danger === 'write' ? '#b91c1c' : '#475569',
                      border: '1px solid #e2e8f0',
                    }}
                  >
                    {item.danger}
                  </span>
                )}
              </div>

              <div className="memory-secondary" style={{ fontSize: '0.75rem' }}>
                <span>Created: {formatLocalTime(item.created_at)}</span>
                {item.expires_at ? <span> · Expires: {formatLocalTime(item.expires_at)}</span> : null}
              </div>
            </div>

            {/* Routine details */}
            {item.routine_name && (
              <div style={{ fontSize: '0.82rem', color: '#4338ca' }}>
                Routine: <strong>{item.routine_name}</strong>
                {item.routine_id ? ` (#${item.routine_id})` : ''}
              </div>
            )}

            {/* Webhook review warning */}
            {item.source === 'webhook' && (
              <div
                className="memory-confirm-warning"
                style={{
                  fontSize: '0.82rem',
                  padding: '6px 10px',
                  background: '#fffbeb',
                  border: '1px solid #fef3c7',
                  borderRadius: 6,
                }}
              >
                ⚠️ Triggered by an external webhook - review carefully
              </div>
            )}

            {/* Main content: Read-only draft OR Edit Form OR Raw Arg */}
            {!isEditing ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                {item.draft ? (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                    {Object.entries(item.draft).map(([field, val]) => (
                      <div key={field}>
                        <span
                          style={{
                            fontWeight: 600,
                            fontSize: '0.75rem',
                            color: 'var(--muted)',
                            textTransform: 'uppercase',
                            letterSpacing: '0.04em',
                          }}
                        >
                          {field}
                        </span>
                        <pre
                          style={{
                            margin: '3px 0 0',
                            whiteSpace: 'pre-wrap',
                            wordBreak: 'break-word',
                            background: '#f8fafc',
                            border: '1px solid #e2e8f0',
                            padding: '6px 10px',
                            borderRadius: 6,
                            fontSize: '0.84rem',
                            color: '#1e293b',
                            fontFamily: 'inherit',
                          }}
                        >
                          {val}
                        </pre>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div>
                    <span
                      style={{
                        fontWeight: 600,
                        fontSize: '0.75rem',
                        color: 'var(--muted)',
                        textTransform: 'uppercase',
                      }}
                    >
                      {item.kind === 'tool' ? 'Arguments' : 'Job Details'}
                    </span>
                    <pre
                      style={{
                        margin: '3px 0 0',
                        whiteSpace: 'pre-wrap',
                        wordBreak: 'break-word',
                        background: '#f8fafc',
                        border: '1px solid #e2e8f0',
                        padding: '6px 10px',
                        borderRadius: 6,
                        fontSize: '0.84rem',
                        color: '#1e293b',
                        fontFamily: 'inherit',
                      }}
                    >
                      {item.arg || (item.kind === 'job' ? `${item.action} job ${item.job_id}` : '—')}
                    </pre>
                  </div>
                )}

                {item.redacted && (
                  <div className="memory-secondary" style={{ fontSize: '0.78rem' }}>
                    🔒 Content was redacted; editing is disabled to protect credentials.
                  </div>
                )}
              </div>
            ) : (
              /* Edit Mode */
              <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                {Object.keys(editDraft).map((field) => {
                  const val = editDraft[field] ?? '';
                  const isMultiline = ['body', 'text', 'prompt'].includes(field);
                  const hint = getFieldValidationHint(field, val);

                  return (
                    <div key={field} style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                        <label
                          htmlFor={`field-${item.id}-${field}`}
                          style={{
                            fontWeight: 600,
                            fontSize: '0.75rem',
                            color: 'var(--muted)',
                            textTransform: 'uppercase',
                          }}
                        >
                          {field}
                        </label>
                        {hint && <span className="memory-error">{hint}</span>}
                      </div>

                      {isMultiline ? (
                        <textarea
                          id={`field-${item.id}-${field}`}
                          className="memory-input"
                          style={{ minHeight: 90, resize: 'vertical', width: '100%', boxSizing: 'border-box' }}
                          value={val}
                          onChange={(e) => {
                            setEditDraft((prev) => ({ ...prev, [field]: e.target.value }));
                            setConfirmApproveEditedId(null);
                          }}
                        />
                      ) : (
                        <input
                          id={`field-${item.id}-${field}`}
                          type="text"
                          className="memory-input"
                          style={{ width: '100%', boxSizing: 'border-box' }}
                          value={val}
                          onChange={(e) => {
                            setEditDraft((prev) => ({ ...prev, [field]: e.target.value }));
                            setConfirmApproveEditedId(null);
                          }}
                        />
                      )}
                    </div>
                  );
                })}

                {/* Changed fields summary */}
                {changedFields.length > 0 && (
                  <div
                    style={{
                      background: '#f1f5f9',
                      padding: '8px 12px',
                      borderRadius: 6,
                      fontSize: '0.8rem',
                      display: 'flex',
                      flexDirection: 'column',
                      gap: 4,
                    }}
                  >
                    <strong>Changed fields:</strong>
                    {changedFields.map((cf) => (
                      <div key={cf.field}>
                        <code>{cf.field}</code>: {truncateStr(cf.oldVal)} → {truncateStr(cf.newVal)}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {/* Inline error feedback */}
            {inlineErr && (
              <div
                className="memory-error"
                style={{
                  padding: '6px 10px',
                  background: '#fef2f2',
                  border: '1px solid #fee2e2',
                  borderRadius: 6,
                }}
              >
                {inlineErr}
              </div>
            )}

            {/* Action buttons */}
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', marginTop: 4 }}>
              {!isEditing ? (
                <>
                  <button
                    type="button"
                    className="action-btn"
                    style={{ fontWeight: 600, color: '#15803d' }}
                    disabled={isBusy}
                    onClick={() => void handleApproveAsIs(item)}
                  >
                    {isBusy ? 'Processing…' : 'Approve'}
                  </button>

                  {item.editable && (
                    <button
                      type="button"
                      className="action-btn"
                      disabled={isBusy}
                      onClick={() => startEdit(item)}
                    >
                      Edit
                    </button>
                  )}

                  {confirmRejectId !== item.id ? (
                    <button
                      type="button"
                      className="action-btn memory-btn-danger"
                      disabled={isBusy}
                      onClick={() => setConfirmRejectId(item.id)}
                    >
                      Reject
                    </button>
                  ) : (
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                      <span className="memory-confirm-warning">Confirm reject?</span>
                      <button
                        type="button"
                        className="action-btn memory-btn-danger"
                        disabled={isBusy}
                        onClick={() => void handleReject(item)}
                      >
                        Confirm reject
                      </button>
                      <button
                        type="button"
                        className="action-btn"
                        disabled={isBusy}
                        onClick={() => setConfirmRejectId(null)}
                      >
                        Cancel
                      </button>
                    </div>
                  )}
                </>
              ) : (
                /* Edit Mode Actions */
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                  {confirmApproveEditedId !== item.id ? (
                    <button
                      type="button"
                      className="action-btn"
                      style={{ fontWeight: 600, color: '#1d4ed8' }}
                      disabled={isBusy || Boolean(draftError)}
                      onClick={() => setConfirmApproveEditedId(item.id)}
                    >
                      Approve edited
                    </button>
                  ) : (
                    <button
                      type="button"
                      className="action-btn"
                      style={{ fontWeight: 600, background: '#2563eb', color: '#fff', border: '1px solid #2563eb' }}
                      disabled={isBusy || Boolean(draftError)}
                      onClick={() => void handleApproveEdited(item)}
                    >
                      {isBusy ? 'Processing…' : 'Confirm approve edited'}
                    </button>
                  )}

                  <button
                    type="button"
                    className="action-btn"
                    disabled={isBusy}
                    onClick={cancelEdit}
                  >
                    Cancel
                  </button>
                </div>
              )}
            </div>
          </article>
        );
      })}
    </div>
  );
}
