import { FormEvent, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { channelLabel, ErrorNote, formatDateTime, formatWhen, friendlyError, responseError, type FriendlyError } from '../ux/knowledgeKit';

export type MemoryItem = {
  id: number;
  kind: 'profile' | 'facts' | 'log';
  source?: string;
  text: string;
  day: string;
  created_at: string;
  updated_at: string;
  source_channel: string;
  expires_at?: string | null;
};

type MemoryList = {
  items: MemoryItem[];
  counts: { profile: number; facts?: number; log: number };
  shared: boolean;
  profile_cap: number;
};

type Tier = 'profile' | 'facts' | 'log';

const TIER_LABEL: Record<Tier, string> = { profile: '常用档案', facts: '事实', log: '记忆日志' };
const FACT_MAX = 280;
const LOG_MAX = 4000;

function fromChannel(channel: string) {
  const label = channelLabel(channel);
  if (!label) return '';
  return /^[㐀-鿿]/.test(label) ? `来自${label}` : `来自 ${label}`;
}

/** With `agentId` the panel shows that agent's own memory; without, the operator's. */
export type MemoryPanelProps = { token: string; agentId?: string; agentName?: string };

export function MemoryPanel({ token, agentId = '', agentName = '' }: MemoryPanelProps) {
  const [disabled, setDisabled] = useState(false);
  const disabledRef = useRef(false);
  const [data, setData] = useState<MemoryList | null>(null);
  const [kind, setKind] = useState<'all' | Tier>('all');
  const [newTier, setNewTier] = useState<Tier>('facts');
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editingText, setEditingText] = useState('');
  const [query, setQuery] = useState('');
  const [activeQuery, setActiveQuery] = useState('');
  const [composing, setComposing] = useState(false);
  const [error, setError] = useState<FriendlyError | null>(null);
  const [newText, setNewText] = useState('');
  const [formBusy, setFormBusy] = useState(false);
  const [formError, setFormError] = useState<FriendlyError | null>(null);
  const [savedNote, setSavedNote] = useState('');
  const [confirmId, setConfirmId] = useState<number | null>(null);
  const [deleting, setDeleting] = useState(false);
  const fetchGen = useRef(0);
  const addRef = useRef<HTMLTextAreaElement>(null);

  // The add box starts as one line and grows with the sentence.
  useLayoutEffect(() => {
    const box = addRef.current;
    if (!box) return;
    box.style.height = 'auto';
    box.style.height = `${Math.min(box.scrollHeight + box.offsetHeight - box.clientHeight, 168)}px`;
  }, [newText]);

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
      if (!res.ok) throw await responseError(res);
      const next = (await res.json()) as MemoryList;
      if (gen !== fetchGen.current) return;
      setData(next);
      setError(null);
    } catch (err) {
      if (gen !== fetchGen.current) return;
      setError(friendlyError(err, '没能读取记忆，请稍后重试'));
    }
  }, [token, kind, activeQuery, agentId]);

  useEffect(() => {
    void load();
  }, [load]);

  // Search as you type (after a short pause); Enter searches at once.
  useEffect(() => {
    if (composing) return;
    const next = query.trim();
    if (next === activeQuery) return;
    const timer = window.setTimeout(() => setActiveQuery(next), 350);
    return () => window.clearTimeout(timer);
  }, [query, activeQuery, composing]);

  useEffect(() => {
    if (!savedNote) return;
    const timer = window.setTimeout(() => setSavedNote(''), 4000);
    return () => window.clearTimeout(timer);
  }, [savedNote]);

  const hasFacts = data?.counts.facts !== undefined;
  const limit = hasFacts && newTier === 'log' ? LOG_MAX : FACT_MAX;

  const handleAdd = async (e?: FormEvent) => {
    e?.preventDefault();
    const text = newText.trim();
    if (!text || formBusy) return;
    setFormBusy(true);
    setFormError(null);
    try {
      const res = await fetch('/api/memory', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({ text, ...(agentId ? { agent: agentId } : {}), ...(hasFacts ? { tier: newTier } : {}) }),
      });
      if (!res.ok) throw await responseError(res);
      const respData = await res.json().catch(() => ({}));
      if (respData.ok === false) {
        setFormError(friendlyError(respData.error || '', '没能记住这句话，请换个说法再试'));
        return;
      }
      setNewText('');
      setSavedNote('已记住。');
      await load();
    } catch (err) {
      setFormError(friendlyError(err, '保存失败，请重试'));
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
      if (!res.ok && res.status !== 404) throw await responseError(res);
      setConfirmId(null);
      await load();
    } catch (err) {
      setError(friendlyError(err, '删除失败，请重试'));
    } finally {
      setDeleting(false);
    }
  };

  const handleEdit = async (id: number) => {
    setFormBusy(true);
    try {
      const res = await fetch(`/api/memory/${id}${agentId ? `?agent=${encodeURIComponent(agentId)}` : ''}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({ text: editingText }),
      });
      if (!res.ok) throw await responseError(res);
      setEditingId(null);
      await load();
    } catch (err) {
      setError(friendlyError(err, '修改失败，请重试'));
    } finally {
      setFormBusy(false);
    }
  };

  const clearSearch = () => {
    setQuery('');
    setActiveQuery('');
  };

  if (disabled) {
    return (
      <div className="stream-body kn-page kn-memory">
        <p className="kn-off">记忆功能未开启。</p>
      </div>
    );
  }

  const who = agentName || 'Conveyor';
  const scope = agentId
    ? `这是 ${agentName || '这个助手'} 自己的记忆，其他助手看不到。`
    : data
      ? (data.shared ? '网页、Telegram 和飞书共用这份记忆。' : '这份记忆只在网页控制台使用。')
      : '';
  const items = data?.items ?? [];
  const counts = data?.counts;
  const total = counts ? counts.profile + counts.log + (counts.facts ?? 0) : 0;
  const filters: Array<{ id: 'all' | Tier; label: string; count?: number }> = [
    { id: 'all', label: '全部', count: counts ? total : undefined },
    { id: 'profile', label: TIER_LABEL.profile, count: counts?.profile },
    ...(hasFacts ? [{ id: 'facts' as const, label: TIER_LABEL.facts, count: counts?.facts }] : []),
    { id: 'log', label: TIER_LABEL.log, count: counts?.log },
  ];
  const near = newText.length >= limit - 20;

  return (
    <div className="stream-body kn-page kn-memory">
      {error && <ErrorNote error={error} onRetry={() => void load()} onDismiss={() => setError(null)} />}

      {/* The page header already names the page (记忆 · whose memory), so the intro is one muted paragraph. */}
      <section className="kn-intro" aria-label="说明">
        <p>
          {scope && <span className="kn-intro-scope">{scope}</span>}
          {who} 会在之后的对话里参考这些内容。在对话里说“记住……”也能添加，需要你确认；不会保存密码和密钥。
        </p>
      </section>

      <form className="kn-card kn-memory-add" onSubmit={handleAdd}>
        <label className="kn-label" htmlFor={`kn-memory-new-${agentId || 'me'}`}>添加一条记忆</label>
        <div className="kn-memory-add-row">
          <textarea
            id={`kn-memory-new-${agentId || 'me'}`}
            ref={addRef}
            className="kn-input kn-memory-textarea"
            value={newText}
            rows={1}
            maxLength={limit}
            placeholder="例如：我习惯用中文回复"
            onChange={(e) => {
              setNewText(e.target.value);
              if (formError) setFormError(null);
            }}
            onKeyDown={(e) => {
              if (e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229) return;
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                void handleAdd();
              }
            }}
            aria-describedby={`kn-memory-count-${agentId || 'me'}`}
          />
          <button type="submit" className="kn-btn kn-btn-primary" disabled={formBusy || !newText.trim()}>
            {formBusy ? '保存中…' : '记住'}
          </button>
        </div>
        <div className="kn-memory-add-foot">
          {hasFacts && (
            <label className="kn-inline-select">
              <span>存为</span>
              <select className="kn-input" value={newTier} onChange={(e) => setNewTier(e.target.value as Tier)} aria-label="记忆类型">
                <option value="facts">{TIER_LABEL.facts}</option>
                <option value="profile">{TIER_LABEL.profile}</option>
                <option value="log">日志</option>
              </select>
            </label>
          )}
          <span id={`kn-memory-count-${agentId || 'me'}`} className={`kn-counter${near ? ' is-near' : ''}`}>
            {limit === FACT_MAX ? '一次一句话 · ' : ''}{newText.length} / {limit}
          </span>
        </div>
        {formError && <ErrorNote error={formError} className="kn-error-inline" />}
        {savedNote && <p className="kn-saved" role="status">{savedNote}</p>}
      </form>

      {(total > 0 || activeQuery || kind !== 'all' || query) && <div className="kn-memory-toolbar">
        <div className="kn-segmented" role="group" aria-label="按类型筛选">
          {filters.map((filter) => (
            <button
              key={filter.id}
              type="button"
              aria-pressed={kind === filter.id}
              className={kind === filter.id ? 'is-active' : ''}
              onClick={() => setKind(filter.id)}
            >
              {filter.label}
              {filter.count !== undefined && <span className="kn-seg-count">{filter.count}</span>}
            </button>
          ))}
        </div>
        <form
          role="search"
          className="kn-search"
          onSubmit={(e) => {
            e.preventDefault();
            setActiveQuery(query.trim());
          }}
        >
          <svg className="kn-search-icon" viewBox="0 0 20 20" aria-hidden="true">
            <circle cx="8.5" cy="8.5" r="5.5" fill="none" stroke="currentColor" strokeWidth="1.8" />
            <path d="M12.6 12.6 17 17" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
          </svg>
          <input
            type="search"
            className="kn-input"
            value={query}
            enterKeyHint="search"
            placeholder="搜索记忆（中文或英文都可以）"
            onChange={(e) => setQuery(e.target.value)}
            onCompositionStart={() => setComposing(true)}
            onCompositionEnd={(e) => {
              setComposing(false);
              setQuery(e.currentTarget.value);
            }}
            onKeyDown={(e) => {
              if (e.key === 'Escape' && query) {
                e.preventDefault();
                clearSearch();
              }
            }}
            aria-label="搜索记忆"
          />
        </form>
      </div>}

      {activeQuery && (
        <div className="kn-result-line" role="status">
          <span>{data ? `找到 ${items.length} 条和“${activeQuery}”相关的记忆` : `正在搜索“${activeQuery}”…`}</span>
          <button type="button" className="kn-link" onClick={clearSearch}>清除搜索</button>
        </div>
      )}

      {!data && !error ? (
        <p className="kn-loading">加载中…</p>
      ) : items.length === 0 ? (
        data && (
          <div className="kn-empty">
            {activeQuery ? (
              <p>没有找到和“{activeQuery}”相关的记忆。换个关键词试试。</p>
            ) : kind !== 'all' ? (
              <>
                <p>“{TIER_LABEL[kind]}”里还没有内容。</p>
                <button type="button" className="kn-btn" onClick={() => setKind('all')}>查看全部</button>
              </>
            ) : (
              <>
                <p className="kn-empty-title">还没有记忆。</p>
                <p>在上面写一句希望 {who} 一直记得的事，也可以在对话里直接说“记住……”。</p>
              </>
            )}
          </div>
        )
      ) : (
        <ul className="kn-memory-list" aria-label="记忆列表">
          {items.map((item) => {
            const tier = (TIER_LABEL[item.kind as Tier] ? item.kind : 'log') as Tier;
            const editing = editingId === item.id;
            return (
              <li key={item.id} className="kn-card kn-memory-item">
                {editing ? (
                  <div className="kn-memory-edit">
                    <textarea
                      className="kn-input kn-memory-textarea"
                      rows={2}
                      value={editingText}
                      maxLength={item.kind === 'log' ? LOG_MAX : FACT_MAX}
                      onChange={(e) => setEditingText(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229) return;
                        if (e.key === 'Escape') setEditingId(null);
                        if (e.key === 'Enter' && !e.shiftKey && editingText.trim()) {
                          e.preventDefault();
                          void handleEdit(item.id);
                        }
                      }}
                      aria-label="修改记忆内容"
                      autoFocus
                    />
                    <div className="kn-row-actions">
                      <button type="button" className="kn-btn kn-btn-primary" disabled={formBusy || !editingText.trim()} onClick={() => void handleEdit(item.id)}>
                        {formBusy ? '保存中…' : '保存'}
                      </button>
                      <button type="button" className="kn-btn" onClick={() => setEditingId(null)}>取消</button>
                    </div>
                  </div>
                ) : (
                  <p className="kn-memory-text">{item.text}</p>
                )}
                <div className={`kn-memory-foot${confirmId === item.id ? ' is-confirming' : ''}`}>
                  <div className="kn-meta">
                    <span className={`kn-tag kn-tag-${tier}`}>{TIER_LABEL[tier]}</span>
                    <time dateTime={item.created_at} title={formatDateTime(item.created_at)}>{formatWhen(item.created_at) || item.day}</time>
                    {item.source_channel && <span>{fromChannel(item.source_channel)}</span>}
                    {item.source === 'untrusted' && <span>来自外部内容</span>}
                    {item.expires_at && <span>{formatDateTime(item.expires_at)} 到期</span>}
                    <span className="kn-memory-id" title="在对话里可以用这个编号，例如“忘掉 #12”">#{item.id}</span>
                  </div>
                  {!editing && (
                    <div className="kn-row-actions">
                      {confirmId === item.id ? (
                        <>
                          <span className="kn-confirm-text">删除后无法恢复</span>
                          <button
                            type="button"
                            className="kn-btn kn-btn-danger"
                            disabled={deleting}
                            onClick={() => void handleDelete(item.id)}
                          >
                            {deleting ? '删除中…' : '确定删除'}
                          </button>
                          <button type="button" className="kn-btn" disabled={deleting} onClick={() => setConfirmId(null)}>
                            取消
                          </button>
                        </>
                      ) : (
                        <>
                          {hasFacts && (
                            <button type="button" className="kn-btn kn-btn-quiet" onClick={() => { setEditingId(item.id); setEditingText(item.text); setConfirmId(null); }}>
                              编辑
                            </button>
                          )}
                          <button
                            type="button"
                            className="kn-btn kn-btn-quiet kn-btn-danger-text"
                            aria-label={`删除这条记忆：${item.text.slice(0, 24)}`}
                            onClick={() => setConfirmId(item.id)}
                          >
                            删除
                          </button>
                        </>
                      )}
                    </div>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}

      {data && !hasFacts && total > 0 && (
        <p className="kn-footnote">最先记住的 {data.profile_cap} 条是常用档案，每次对话都会参考；之后的存进记忆日志，需要时再查。</p>
      )}
    </div>
  );
}
