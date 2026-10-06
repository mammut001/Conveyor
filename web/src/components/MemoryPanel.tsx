import { FormEvent, useCallback, useEffect, useRef, useState } from 'react';

export type MemoryItem = {
  id: number;
  kind: 'profile' | 'log';
  text: string;
  day: string;
  created_at: string;
  updated_at: string;
  source_channel: string;
};

type MemoryList = {
  items: MemoryItem[];
  counts: { profile: number; log: number };
  shared: boolean;
  profile_cap: number;
};

/** With `agentId` the panel shows that agent's own memory; without, the operator's. */
export type MemoryPanelProps = { token: string; agentId?: string; agentName?: string };

export function MemoryPanel({ token, agentId = '', agentName = '' }: MemoryPanelProps) {
  const [disabled, setDisabled] = useState(false);
  const disabledRef = useRef(false);
  const [data, setData] = useState<MemoryList | null>(null);
  const [kind, setKind] = useState<'all' | 'profile' | 'log'>('all');
  const [query, setQuery] = useState('');
  const [activeQuery, setActiveQuery] = useState('');
  const [error, setError] = useState('');
  const [newText, setNewText] = useState('');
  const [formBusy, setFormBusy] = useState(false);
  const [formError, setFormError] = useState('');
  const [confirmId, setConfirmId] = useState<number | null>(null);
  const [deleting, setDeleting] = useState(false);
  const fetchGen = useRef(0);

  const load = useCallback(async () => {
    if (disabledRef.current || !token) return;
    const gen = ++fetchGen.current;
    try {
      const params = new URLSearchParams({ limit: '200' });
      if (kind !== 'all') params.set('kind', kind);
      if (activeQuery.trim()) params.set('q', activeQuery.trim());
      if (agentId) params.set('agent', agentId);
      const res = await fetch(`/api/memory?${params.toString()}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.status === 409) {
        disabledRef.current = true;
        setDisabled(true);
        return;
      }
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.error || `Failed to load memory (${res.status})`);
      }
      const next = (await res.json()) as MemoryList;
      if (gen !== fetchGen.current) return;
      setData(next);
      setError('');
    } catch (err) {
      if (gen !== fetchGen.current) return;
      setError(err instanceof Error ? err.message : 'Failed to load memory');
    }
  }, [token, kind, activeQuery, agentId]);

  useEffect(() => {
    void load();
  }, [load]);

  const handleAdd = async (e: FormEvent) => {
    e.preventDefault();
    const text = newText.trim();
    if (!text) return;
    setFormBusy(true);
    setFormError('');
    try {
      const res = await fetch('/api/memory', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify(agentId ? { text, agent: agentId } : { text }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.error || `Failed to save (${res.status})`);
      }
      const respData = await res.json().catch(() => ({}));
      if (respData.ok === false) {
        setFormError(respData.error || 'Failed to save');
        return;
      }
      setNewText('');
      await load();
    } catch (err) {
      setFormError(err instanceof Error ? err.message : 'Failed to save');
    } finally {
      setFormBusy(false);
    }
  };

  const handleDelete = async (id: number) => {
    setDeleting(true);
    try {
      const res = await fetch(`/api/memory/${id}${agentId ? `?agent=${encodeURIComponent(agentId)}` : ''}`, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok && res.status !== 404) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.error || `Failed to delete (${res.status})`);
      }
      setConfirmId(null);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete');
    } finally {
      setDeleting(false);
    }
  };

  if (disabled) {
    return (
      <div className="stream-body" style={{ padding: '24px 20px' }}>
        <div className="job-notice" style={{ maxWidth: 640 }}>
          <strong>Long-term memory is disabled</strong>
          <span>Set <code>CONVEYOR_LONG_TERM_MEMORY=true</code> on the server to enable durable memory.</span>
        </div>
      </div>
    );
  }

  const items = data?.items ?? [];
  return (
    <div className="stream-body" style={{ display: 'flex', flexDirection: 'column', height: '100%', overflowY: 'auto', padding: 16, gap: 16 }}>
      {error && (
        <div className="error-banner global" style={{ margin: 0 }}>
          {error}
          <button type="button" onClick={() => setError('')}>×</button>
        </div>
      )}

      <section className="memory-card">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}>
          <h3 style={{ margin: 0, fontSize: '0.95rem', fontWeight: 600 }}>
            {agentId && agentName ? `${agentName}'s memory` : 'Long-term memory'} · profile {data?.counts.profile ?? 0}/{data?.profile_cap ?? 8} · log {data?.counts.log ?? 0}
          </h3>
          <span className="memory-secondary" style={{ fontSize: '0.75rem' }}>
            {data ? (data.shared ? 'Shared across Web, Telegram and Feishu' : 'Web Console memory only') : ''}
          </span>
        </div>
        <p className="memory-secondary" style={{ fontSize: '0.8rem', margin: '8px 0 0' }}>
          Facts you asked Conveyor to keep. In chat, say “记住 …” / “remember …” (needs approval). Secrets are refused.
        </p>
      </section>

      <section className="memory-card" style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        <form onSubmit={handleAdd} style={{ display: 'flex', gap: 8 }}>
          <input
            type="text"
            className="memory-input"
            value={newText}
            maxLength={280}
            placeholder="Add one fact (one sentence, max 280 chars)"
            onChange={(e) => setNewText(e.target.value)}
            style={{ flex: 1 }}
            aria-label="New memory"
          />
          <button type="submit" className="action-btn" disabled={formBusy || !newText.trim()}>
            {formBusy ? 'Saving…' : 'Remember'}
          </button>
        </form>
        {formError && <div className="memory-error">{formError}</div>}

        <form
          onSubmit={(e) => {
            e.preventDefault();
            setActiveQuery(query);
          }}
          style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}
        >
          <select className="memory-input" value={kind} onChange={(e) => setKind(e.target.value as 'all' | 'profile' | 'log')} aria-label="Tier">
            <option value="all">All</option>
            <option value="profile">Profile</option>
            <option value="log">Log</option>
          </select>
          <input
            type="search"
            className="memory-input"
            value={query}
            placeholder="Search (中文 / English)"
            onChange={(e) => setQuery(e.target.value)}
            style={{ flex: 1, minWidth: 160 }}
            aria-label="Search memory"
          />
          <button type="submit" className="action-btn">Search</button>
          {activeQuery && (
            <button
              type="button"
              className="action-btn"
              onClick={() => {
                setQuery('');
                setActiveQuery('');
              }}
            >
              Clear
            </button>
          )}
        </form>
      </section>

      <section style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {items.length === 0 ? (
          <p className="memory-secondary" style={{ fontSize: '0.85rem' }}>
            {activeQuery ? `No memory matches “${activeQuery}”.` : 'No long-term memory yet.'}
          </p>
        ) : (
          items.map((item) => (
            <article key={item.id} className="memory-card" style={{ padding: 12 }}>
              <header style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                <div className="memory-secondary" style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: '0.75rem' }}>
                  <strong style={{ color: 'inherit' }}>#{item.id}</strong>
                  <span className={`status-badge ${item.kind === 'profile' ? 'completed' : 'queued'}`} style={{ padding: '1px 6px' }}>
                    {item.kind}
                  </span>
                  <span>{item.day}</span>
                  {item.source_channel && <span>via {item.source_channel}</span>}
                </div>
                {confirmId === item.id ? (
                  <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                    <span className="memory-confirm-warning">Delete permanently?</span>
                    <button
                      type="button"
                      className="action-btn memory-btn-danger"
                      style={{ fontSize: '0.8rem', padding: '3px 10px' }}
                      disabled={deleting}
                      onClick={() => void handleDelete(item.id)}
                    >
                      {deleting ? 'Deleting…' : 'Confirm delete'}
                    </button>
                    <button type="button" className="action-btn" style={{ fontSize: '0.8rem', padding: '3px 10px' }} disabled={deleting} onClick={() => setConfirmId(null)}>
                      Cancel
                    </button>
                  </div>
                ) : (
                  <button type="button" className="action-btn" style={{ fontSize: '0.8rem', padding: '3px 10px' }} onClick={() => setConfirmId(item.id)}>
                    Delete
                  </button>
                )}
              </header>
              <div style={{ fontSize: '0.9rem', lineHeight: 1.5, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{item.text}</div>
            </article>
          ))
        )}
      </section>
    </div>
  );
}
