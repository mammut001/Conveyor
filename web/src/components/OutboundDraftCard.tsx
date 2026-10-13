import React, { useState } from 'react';

type Card = {
  id: string;
  tool_name?: string;
  draft?: Record<string, string> | null;
  draft_hash?: string;
  expires_at?: number | string;
  save_only?: boolean;
};

export function OutboundDraftCard({ item, token, onDone }: {
  item: Card; token: string; onDone: () => void;
}) {
  const [draft, setDraft] = useState<Record<string, string>>({ ...(item.draft || {}) });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [warning, setWarning] = useState('');
  const expiry = item.expires_at ?
    new Date(typeof item.expires_at === 'number' ? item.expires_at * 1000 : item.expires_at) : null;
  const expired = expiry ? expiry.getTime() <= Date.now() : false;

  const submit = async (decision: 'send' | 'deny' | 'save') => {
    setBusy(true);
    setError('');
    try {
      const response = await fetch(`/api/approval-inbox/${encodeURIComponent(item.id)}/draft`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ expected_hash: item.draft_hash, draft, decision }),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
      if (result.status === 'needs_reconciliation') {
        setWarning('Send outcome uncertain. Do not retry; reconcile with the destination first.');
      }
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Draft request failed');
    } finally {
      setBusy(false);
    }
  };

  return <article className="memory-card" style={{ padding: 16, display: 'flex', flexDirection: 'column', gap: 12 }}>
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
      <strong>Outbound draft · {item.tool_name}</strong>
      <span className="memory-secondary">{expiry ? `Expires ${expiry.toLocaleString()}` : ''}</span>
    </div>
    <p className="memory-secondary">Review or edit every field. Nothing will be sent until you press Send. One shot only.</p>
    {Object.entries(draft).map(([name, value]) => (
      <label key={name} style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
        <span style={{ textTransform: 'uppercase', fontSize: 12, fontWeight: 600 }}>{name}</span>
        {name === 'body' || name === 'payload' ?
          <textarea className="memory-input" style={{ minHeight: 120, width: '100%' }}
            value={value} disabled={busy || expired}
            onChange={e => setDraft(d => ({ ...d, [name]: e.target.value }))} /> :
          <input className="memory-input" style={{ width: '100%' }} value={value}
            disabled={busy || expired} onChange={e => setDraft(d => ({ ...d, [name]: e.target.value }))} />
        }
      </label>
    ))}
    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
      <button type="button" className="action-btn" disabled={busy || expired}
        onClick={() => void submit('send')}>{busy ? 'Processing…' : 'Send'}</button>
      {item.save_only && <button type="button" className="action-btn" disabled={busy || expired}
        onClick={() => void submit('save')}>Save as draft only</button>}
      <button type="button" className="action-btn memory-btn-danger" disabled={busy || expired}
        onClick={() => void submit('deny')}>Deny</button>
    </div>
    {error && <div role="alert" className="memory-error">{error}</div>}
    {warning && <div role="alert" className="memory-error">{warning}</div>}
    {expired && <div role="alert" className="memory-error">Draft expired; request a new one.</div>}
  </article>;
}
