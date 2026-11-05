import React, { useState } from 'react';
import { ChatRequestError, describeChatError, formatChatTime } from './chatFormat';

const FIELD_LABELS: Record<string, string> = {
  to: '收件人', cc: '抄送', bcc: '密送', subject: '主题', body: '正文',
  repo: '仓库', number: '编号', title: '标题', payload: '内容',
};

type Card = {
  id: string;
  tool_name?: string;
  draft?: Record<string, string> | null;
  draft_hash?: string;
  expires_at?: number | string;
  save_only?: boolean;
  repo_editable?: boolean;
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
      if (!response.ok) throw new ChatRequestError(result.error || '', response.status);
      if (result.status === 'needs_reconciliation') {
        setWarning('无法确认是否已经发出。请不要重试，先到对方那里核对一下。');
      }
      onDone();
    } catch (e) {
      setError(describeChatError(e, { fallback: '草稿没有处理成功，请再试一次', network: '网络中断，草稿没有处理成功，请再试一次' }).message);
    } finally {
      setBusy(false);
    }
  };

  return <article className="memory-card" style={{ padding: 16, display: 'flex', flexDirection: 'column', gap: 12 }}>
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
      <strong>待发送草稿 · {item.tool_name}</strong>
      <span className="memory-secondary">{expiry ? `${formatChatTime(expiry.getTime())} 前有效` : ''}</span>
    </div>
    <p className="memory-secondary">请检查或修改每一项。按「发送」之前不会发出任何内容，而且只能发送一次。</p>
    {Object.entries(draft).map(([name, value]) => (
      <label key={name} style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
        <span style={{ fontSize: 12, fontWeight: 600 }}>{FIELD_LABELS[name] || name}</span>
        {name === 'body' || name === 'payload' ?
          <textarea className="memory-input" style={{ minHeight: 120, width: '100%' }}
            value={value} disabled={busy || expired}
            onChange={e => setDraft(d => ({ ...d, [name]: e.target.value }))} /> :
          <input className="memory-input" style={{ width: '100%' }} value={value}
            disabled={busy || expired || (name === 'repo' && !item.repo_editable)}
            onChange={e => setDraft(d => ({ ...d, [name]: e.target.value }))} />
        }
      </label>
    ))}
    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
      <button type="button" className="action-btn" disabled={busy || expired}
        onClick={() => void submit('send')}>{busy ? '处理中…' : '发送'}</button>
      {item.save_only && <button type="button" className="action-btn" disabled={busy || expired}
        onClick={() => void submit('save')}>只保存为草稿</button>}
      <button type="button" className="action-btn memory-btn-danger" disabled={busy || expired}
        onClick={() => void submit('deny')}>拒绝发送</button>
    </div>
    {error && <div role="alert" className="memory-error">{error}</div>}
    {warning && <div role="alert" className="memory-error">{warning}</div>}
    {expired && <div role="alert" className="memory-error">草稿已过期，请重新生成。</div>}
  </article>;
}
