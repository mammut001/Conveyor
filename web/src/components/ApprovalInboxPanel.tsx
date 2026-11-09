import React, { useCallback, useEffect, useRef, useState } from 'react';
import { isStale } from '../approvalFreshness';
import { OutboundDraftCard } from './OutboundDraftCard';
import { BAD_SHAPE_MESSAGE, formatDateTime, friendlyError, useLoadingTimeout } from '../ux/knowledgeKit';

export type ApprovalInboxItem = {
  id: string;
  kind: 'tool' | 'job' | 'relay' | 'reconciliation';
  source: 'chat' | 'routine' | 'webhook' | 'job' | 'telegram' | 'feishu' | string;
  action?: string; // apply | discard for job
  job_id?: string;
  tool_name?: string;
  summary?: string;
  danger?: string; // write | write_safe
  arg?: string;
  draft?: Record<string, string> | null;
  draft_card?: boolean;
  draft_hash?: string;
  save_only?: boolean;
  editable: boolean;
  redacted?: boolean;
  g3_preview?: { url: string; target: string; method: string; fields: [string, string][]; screenshot_sha256: string };
  g3_hash?: string;
  g3_screenshot?: string;
  created_at: string | number;
  expires_at?: string | number;
  session_id?: string;
  routine_id?: number | null;
  routine_name?: string | null;
  origin_channel?: string;
};

export type ApprovalInboxPanelProps = {
  token: string;
  onApprovalDecided?: () => void;
  onPendingCountChange?: (count: number) => void;
};

function formatLocalTime(val?: string | number | null) {
  if (!val) return '—';
  return formatDateTime(val) || String(val);
}

const SOURCE_LABEL: Record<string, string> = {
  chat: '对话', routine: '定时任务', webhook: 'Webhook', job: '任务', telegram: 'Telegram', feishu: '飞书',
};
const DANGER_LABEL: Record<string, string> = { write: '会写入', write_safe: '安全写入' };
const FIELD_LABEL: Record<string, string> = {
  to: '收件人', subject: '主题', body: '正文', title: '标题', text: '内容', prompt: '内容',
  number: '号码', name: '名称', cron: '运行时间（cron）',
};

function truncateStr(s: string, maxLen = 30): string {
  if (!s) return '""';
  if (s.length <= maxLen) return JSON.stringify(s);
  return JSON.stringify(s.slice(0, maxLen) + '…');
}

function getFieldValidationHint(field: string, value: string): string | null {
  if (typeof value !== 'string') return '必须是文字';
  const SINGLE_LINE = ['to', 'subject', 'number', 'title', 'cron', 'name'];
  if (SINGLE_LINE.includes(field)) {
    if (value.includes('|')) return '不能包含竖线 (|)';
    if (value.includes('\n') || value.includes('\r')) return '不能换行';
  }
  if (field === 'prompt' && value.includes('|')) {
    return '内容不能包含竖线 (|)';
  }
  if (field === 'number') {
    if (!value.trim() || !/^\d+$/.test(value.trim())) return '只能填数字';
  }
  if (field === 'to') {
    const parts = value.split(',').map((s) => s.trim());
    if (!parts.length || parts.some((p) => !p)) return '请填写邮箱地址，多个用逗号分开';
    const emailPattern = /^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$/;
    for (const p of parts) {
      const clean = p.includes('<') && p.includes('>') ? p.replace(/^.*<([^>]+)>.*$/, '$1') : p;
      if (!emailPattern.test(clean)) return `邮箱地址不对：${p}`;
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
    return `最多 ${cap} 个字（现在 ${value.length}）`;
  }
  if (['to', 'subject', 'title', 'text', 'cron'].includes(field) && !value.trim()) {
    return '不能为空';
  }
  if (field === 'prompt' && !value.trim()) {
    return '不能为空';
  }
  return null;
}

function getDraftValidationError(draft: Record<string, string>): string | null {
  for (const [field, value] of Object.entries(draft)) {
    const hint = getFieldValidationHint(field, value);
    if (hint) return `${FIELD_LABEL[field] || field}：${hint}`;
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
    const id = new URLSearchParams(window.location.search).get('approval')
    if (!id || !items.some(item => item.id === id)) return
    const timer = window.setTimeout(() => {
      document.getElementById('pwa-review-' + id)?.scrollIntoView({ block: 'center', behavior: 'smooth' })
    }, 0)
    return () => window.clearTimeout(timer)
  }, [items]);
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

  useLoadingTimeout(loading, () => { setGlobalError('加载太久了，请重试'); setLoading(false); });

  const fetchItems = useCallback(async () => {
    const gen = ++fetchGen.current;
    try {
      const res = await fetch('/api/approval-inbox', {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (isStale(gen, fetchGen.current)) return;
      if (!res.ok) {
        if (res.status === 409) {
          setGlobalError('审批收件箱功能未开启。');
          setItems([]);
          onPendingCountChange?.(0);
          setLoading(false);
          return;
        }
        throw new Error(`HTTP ${res.status}`);
      }
      const data = await res.json().catch(() => null);
      if (isStale(gen, fetchGen.current)) return;
      if (!data || typeof data !== 'object' || !Array.isArray(data.items)) {
        setGlobalError(BAD_SHAPE_MESSAGE);
        setLoading(false);
        return;
      }
      const fetchedItems: ApprovalInboxItem[] = data.items.filter((it: unknown) => it && typeof it === 'object');
      setItems(fetchedItems);
      onPendingCountChange?.(Number(data.counts?.total) || fetchedItems.length);
      setGlobalError('');
      setLoading(false);
    } catch (err) {
      if (isStale(gen, fetchGen.current)) return;
      setGlobalError(friendlyError(err, '没能读取待批准的事项，请稍后重试').message);
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
    if (item.tool_name === 'browser.g3_dispatch' &&
        (!item.g3_preview || !item.g3_hash || !item.g3_screenshot)) {
      setInlineErrors(prev => ({ ...prev, [item.id]: '预览不可用，请拒绝后重新准备' }));
      return;
    }
    setBusyMap((prev) => ({ ...prev, [item.id]: true }));
    setInlineErrors((prev) => ({ ...prev, [item.id]: '' }));
    try {
      const bodyPayload = item.tool_name === 'browser.g3_dispatch'
        ? { expected_arg: item.kind === 'tool' ? item.arg : undefined, expected_hash: item.g3_hash }
        : item.kind === 'tool' ? { expected_arg: item.arg } : {};
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
        [item.id]: friendlyError(err, '批准没有成功，请重试').message,
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
        [item.id]: friendlyError(err, '批准没有成功，请重试').message,
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
        [item.id]: friendlyError(err, '拒绝没有成功，请重试').message,
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
      case 'telegram':
        return { background: '#f0f9ff', color: '#0284c7', border: '1px solid #bae6fd' };
      case 'feishu':
        return { background: '#f0fdf4', color: '#059669', border: '1px solid #a7f3d0' };
      case 'chat':
      default:
        return { background: '#f0fdf4', color: '#15803d', border: '1px solid #bbf7d0' };
    }
  };

  if (loading && items.length === 0) {
    return (
      <div style={{ padding: 24, textAlign: 'center', color: 'var(--muted)' }}>
        加载中…
      </div>
    );
  }

  return (
    <div style={{ padding: '16px 20px', display: 'flex', flexDirection: 'column', gap: 14 }}>
      {globalError && <div className="error-banner" role="alert">{globalError}{' '}<button type="button" className="kn-btn" onClick={() => { setGlobalError(''); void fetchItems(); }}>重试</button></div>}

      {items.length === 0 && !globalError && (
        <div className="empty" style={{ padding: '36px 12px', textAlign: 'center' }}>
          现在没有需要你批准的事项。
        </div>
      )}

      {items.map((item) => {
        if (item.draft_card && item.draft && item.draft_hash) {
          return <div id={'pwa-review-' + item.id} key={item.id}><OutboundDraftCard item={item} token={token} onDone={() => {
            fetchGen.current++;
            dropItem(item.id);
            onApprovalDecided?.();
            void fetchItems();
          }} /></div>;
        }
        const isEditing = editingId === item.id;
        const isBusy = Boolean(busyMap[item.id]);
        const changedFields = isEditing ? getChangedFields() : [];
        const draftError = isEditing ? getDraftValidationError(editDraft) : null;
        const inlineErr = inlineErrors[item.id];

        return (
          <article
            key={item.id}
            id={'pwa-review-' + item.id}
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
                    ...sourceBadgeStyle(item.source),
                  }}
                >
                  {SOURCE_LABEL[item.source] || item.source}
                </span>

                <strong style={{ fontSize: '0.95rem' }}>
                  {item.kind === 'tool' || item.kind === 'relay' || item.kind === 'reconciliation' ? item.tool_name : item.action === 'apply' ? '应用改动' : item.action === 'discard' ? '放弃改动' : `任务${item.action ? ` ${item.action}` : '审批'}`}
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
                    {DANGER_LABEL[item.danger] || item.danger}
                  </span>
                )}
              </div>

              <div className="memory-secondary" style={{ fontSize: '0.75rem' }}>
                <span>创建于 {formatLocalTime(item.created_at)}</span>
                {item.expires_at ? <span> · {formatLocalTime(item.expires_at)} 过期</span> : null}
              </div>
            </div>

            {/* Routine details */}
            {item.routine_name && (
              <div style={{ fontSize: '0.82rem', color: '#4338ca' }}>
                定时任务：<strong>{item.routine_name}</strong>
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
                ⚠️ 由外部 Webhook 触发，请仔细核对
              </div>
            )}

            {/* Relay origin notice */}
            {item.kind === 'relay' && (
              <div
                style={{
                  fontSize: '0.82rem',
                  padding: '6px 10px',
                  background: '#f8fafc',
                  border: '1px solid #e2e8f0',
                  borderRadius: 6,
                  color: '#475569',
                }}
              >
                ℹ️ 跨端中继审批：将在原会话（{item.origin_channel === 'telegram' ? 'Telegram' : item.origin_channel === 'feishu' ? '飞书' : (item.origin_channel || item.source)}）中执行
              </div>
            )}

            {item.tool_name === 'browser.g3_dispatch' && (
              <div style={{ padding: 12, border: '2px solid #c2410c', borderRadius: 8 }}>
                <strong>{item.kind === 'reconciliation' ? '提交结果不确定，需要人工核对；不要重试' : '需要你本人单独确认（5 分钟内有效）'}</strong>
                {!item.g3_preview || !item.g3_hash || !item.g3_screenshot ? (
                  <p>预览不可用。请拒绝这次审批，再重新准备。</p>
                ) : (
                  <div style={{ display: 'grid', gap: 8, marginTop: 8 }}>
                    <div><strong>操作：</strong> {item.g3_preview.method} {item.g3_preview.target}</div>
                    <div><strong>来源页面：</strong> {item.g3_preview.url}</div>
                    <div><strong>将原样提交的字段：</strong></div>
                    <table><tbody>
                      {item.g3_preview.fields.map(([name, value], i) => (
                        <tr key={i}><th scope="row" style={{ textAlign: 'left', paddingRight: 12 }}>{name}</th><td>{value}</td></tr>
                      ))}
                    </tbody></table>
                    <div style={{ overflowWrap: 'anywhere' }}><strong>SHA-256:</strong> {item.g3_hash}</div>
                    <img src={item.g3_screenshot} alt="批准前截取的页面截图"
                         style={{ maxWidth: '100%', maxHeight: 400, objectFit: 'contain' }} />
                  </div>
                )}
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
                          {FIELD_LABEL[field] || field}
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
                      {item.kind === 'tool' ? '参数' : '任务详情'}
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
                      {item.arg || (item.kind === 'job' ? `${item.action === 'apply' ? '应用改动' : item.action === 'discard' ? '放弃改动' : item.action} · 任务 ${item.job_id}` : '—')}
                    </pre>
                  </div>
                )}

                {item.redacted && (
                  <div className="memory-secondary" style={{ fontSize: '0.78rem' }}>
                    🔒 内容含有敏感信息已被遮盖，为保护密钥不能编辑。
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
                          {FIELD_LABEL[field] || field}
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
                    <strong>改动的字段：</strong>
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

            {/* Reconciliation alerts never have approve/retry controls. */}
            {item.kind !== 'reconciliation' && <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', marginTop: 4 }}>
              {!isEditing ? (
                <>
                  <button
                    type="button"
                    className="action-btn"
                    style={{ fontWeight: 600, color: '#15803d' }}
                    disabled={isBusy}
                    onClick={() => void handleApproveAsIs(item)}
                  >
                    {isBusy ? '处理中…' : '批准'}
                  </button>

                  {item.editable && (
                    <button
                      type="button"
                      className="action-btn"
                      disabled={isBusy}
                      onClick={() => startEdit(item)}
                    >
                      编辑
                    </button>
                  )}

                  {confirmRejectId !== item.id ? (
                    <button
                      type="button"
                      className="action-btn memory-btn-danger"
                      disabled={isBusy}
                      onClick={() => setConfirmRejectId(item.id)}
                    >
                      拒绝
                    </button>
                  ) : (
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                      <span className="memory-confirm-warning">确定拒绝？</span>
                      <button
                        type="button"
                        className="action-btn memory-btn-danger"
                        disabled={isBusy}
                        onClick={() => void handleReject(item)}
                      >
                        确定拒绝
                      </button>
                      <button
                        type="button"
                        className="action-btn"
                        disabled={isBusy}
                        onClick={() => setConfirmRejectId(null)}
                      >
                        取消
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
                      按修改后的内容批准
                    </button>
                  ) : (
                    <button
                      type="button"
                      className="action-btn"
                      style={{ fontWeight: 600, background: '#2563eb', color: '#fff', border: '1px solid #2563eb' }}
                      disabled={isBusy || Boolean(draftError)}
                      onClick={() => void handleApproveEdited(item)}
                    >
                      {isBusy ? '处理中…' : '确定按修改后的内容批准'}
                    </button>
                  )}

                  <button
                    type="button"
                    className="action-btn"
                    disabled={isBusy}
                    onClick={cancelEdit}
                  >
                    取消
                  </button>
                </div>
              )}
            </div>}
          </article>
        );
      })}
    </div>
  );
}
