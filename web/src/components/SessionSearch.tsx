import { FormEvent, useEffect, useRef, useState } from 'react';
import { formatDateTime } from '../ux/knowledgeKit';

// 「搜索过去的对话」 on the 记忆 page: full-text search over earlier conversations
// (/api/memory/sessions/search). Snippets are plain text and rendered as React text
// nodes (React escapes them); the query is highlighted with <mark>, never with raw HTML.

export type SessionSearchResult = {
  session_id: string;
  title: string;
  channel: string;
  created_at: string;
  message_id?: number | string;
  role?: string;
  snippet: string;
  score?: number;
};

export const SEARCH_MAX_CHARS = 200;
const SEARCH_LIMIT = 20;
const DEBOUNCE_MS = 400;
const HAS_CJK = /[㐀-鿿]/;

/** Where a conversation happened, in Chinese. */
export function searchChannelLabel(channel?: string | null): string {
  const value = (channel || '').toLowerCase();
  if (value === 'web' || value === 'webchat') return '网页';
  if (value === 'telegram') return '电报';
  if (value === 'feishu' || value === 'lark') return '飞书';
  if (value === 'agent') return '助手对话';
  if (value.startsWith('supwt') || value === 'supervisor') return '项目';
  return '其他渠道';
}

function roleLabel(role?: string): string {
  if (role === 'user') return '你说的';
  if (role === 'assistant') return '它回复的';
  return '';
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** Split `text` into plain and highlighted parts for every query term (case-insensitive). */
export function highlightParts(text: string, query: string): Array<{ text: string; hit: boolean }> {
  const whole = query.trim();
  const terms = Array.from(new Set([whole, ...whole.split(/\s+/)].filter(Boolean))).sort((a, b) => b.length - a.length);
  if (!text || !terms.length) return [{ text, hit: false }];
  const pattern = new RegExp(`(${terms.map(escapeRegExp).join('|')})`, 'gi');
  return text.split(pattern).filter(Boolean).map((part) => ({
    text: part,
    hit: terms.some((term) => term.toLowerCase() === part.toLowerCase()),
  }));
}

function Highlighted({ text, query }: { text: string; query: string }) {
  return (
    <>
      {highlightParts(text, query).map((part, index) => (part.hit
        ? <mark key={index} className="kn-hit">{part.text}</mark>
        : <span key={index}>{part.text}</span>))}
    </>
  );
}

type SearchState =
  | { status: 'idle' }
  | { status: 'loading'; query: string }
  | { status: 'done'; query: string; results: SessionSearchResult[] }
  | { status: 'error'; query: string; message: string };

export type SessionSearchProps = {
  token: string;
  /** Opens a conversation the same way the 对话 / 任务 lists do. */
  onOpenSession?: (sessionId: string) => void;
};

export function SessionSearch({ token, onOpenSession }: SessionSearchProps) {
  const [query, setQuery] = useState('');
  const [composing, setComposing] = useState(false);
  // The search that actually runs; `nonce` repeats the same words on 搜索.
  const [active, setActive] = useState<{ q: string; nonce: number }>({ q: '', nonce: 0 });
  const [state, setState] = useState<SearchState>({ status: 'idle' });
  const fetchGen = useRef(0);

  // Search after a short pause in typing; 搜索 / Enter searches at once.
  useEffect(() => {
    if (composing) return;
    const next = query.trim().slice(0, SEARCH_MAX_CHARS);
    if (next === active.q) return;
    const timer = window.setTimeout(() => setActive((prev) => ({ q: next, nonce: prev.nonce + 1 })), DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [query, composing, active.q]);

  useEffect(() => {
    const q = active.q;
    const gen = ++fetchGen.current;
    if (!q || !token) {
      setState({ status: 'idle' });
      return;
    }
    setState({ status: 'loading', query: q });
    const controller = new AbortController();
    const params = new URLSearchParams({ q, limit: String(SEARCH_LIMIT) });
    void (async () => {
      try {
        const res = await fetch(`/api/memory/sessions/search?${params.toString()}`, {
          headers: { Authorization: `Bearer ${token}` },
          signal: controller.signal,
        });
        const body = await res.json().catch(() => null) as { results?: unknown; error?: unknown } | null;
        if (gen !== fetchGen.current) return;
        if (!res.ok) {
          const raw = body && typeof body.error === 'string' ? body.error.trim() : '';
          const message = raw && HAS_CJK.test(raw) ? raw
            : res.status === 401 ? '登录已失效，请重新解锁控制台'
              : res.status >= 500 ? '服务器出错了，请稍后重试'
                : '搜索没有成功，请稍后再试';
          setState({ status: 'error', query: q, message });
          return;
        }
        if (!body || !Array.isArray(body.results)) {
          setState({ status: 'error', query: q, message: '搜索结果的格式不对，请稍后重试' });
          return;
        }
        const results = (body.results as unknown[]).filter((item): item is SessionSearchResult =>
          !!item && typeof item === 'object' && typeof (item as SessionSearchResult).session_id === 'string');
        setState({ status: 'done', query: q, results });
      } catch (err) {
        if (controller.signal.aborted || gen !== fetchGen.current) return;
        setState({ status: 'error', query: q, message: err instanceof TypeError ? '网络连接失败，请检查网络后重试' : '搜索没有成功，请稍后再试' });
      }
    })();
    return () => controller.abort();
  }, [active, token]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const next = query.trim().slice(0, SEARCH_MAX_CHARS);
    if (!next) return;
    setActive((prev) => ({ q: next, nonce: prev.nonce + 1 }));
  };

  const clear = () => {
    setQuery('');
    setActive((prev) => ({ q: '', nonce: prev.nonce + 1 }));
  };

  const trimmed = query.trim();

  return (
    <div className="kn-session-search">
      <form className="kn-session-search-form" onSubmit={submit}>
        <label className="sr-only" htmlFor="kn-session-search-input">搜索过去的对话</label>
        <input
          id="kn-session-search-input"
          type="text"
          className="kn-input"
          value={query}
          maxLength={SEARCH_MAX_CHARS}
          enterKeyHint="search"
          autoComplete="off"
          placeholder="输入想找的话，例如：路线图"
          onChange={(e) => setQuery(e.target.value.slice(0, SEARCH_MAX_CHARS))}
          onCompositionStart={() => setComposing(true)}
          onCompositionEnd={(e) => {
            setComposing(false);
            setQuery(e.currentTarget.value.slice(0, SEARCH_MAX_CHARS));
          }}
          onKeyDown={(e) => {
            if (e.key === 'Escape' && query) {
              e.preventDefault();
              clear();
            }
          }}
        />
        <button type="submit" className="kn-btn kn-btn-primary" disabled={!trimmed}>搜索</button>
      </form>
      {query.length >= SEARCH_MAX_CHARS && <p className="kn-counter is-near">最多 {SEARCH_MAX_CHARS} 个字</p>}

      {state.status === 'loading' && <p className="kn-loading" role="status">正在搜索“{state.query}”…</p>}
      {state.status === 'error' && <p className="kn-notes-error" role="alert">{state.message}</p>}
      {state.status === 'done' && (state.results.length === 0 ? (
        <p className="kn-session-search-empty" role="status">没有找到相关的对话</p>
      ) : (
        <>
          <p className="kn-result-line" role="status">
            <span>找到 {state.results.length} 段和“{state.query}”相关的对话</span>
            <button type="button" className="kn-link" onClick={clear}>清除搜索</button>
          </p>
          <ul className="kn-session-hits" aria-label="搜索结果">
            {state.results.map((hit, index) => {
              const body = (
                <>
                  <span className="kn-session-hit-title">{hit.title?.trim() || '未命名的对话'}</span>
                  <span className="kn-meta kn-session-hit-meta">
                    <span className="kn-tag">{searchChannelLabel(hit.channel)}</span>
                    {hit.created_at && <time dateTime={hit.created_at}>{formatDateTime(hit.created_at)}</time>}
                    {roleLabel(hit.role) && <span>{roleLabel(hit.role)}</span>}
                  </span>
                  <span className="kn-session-hit-snippet"><Highlighted text={hit.snippet || ''} query={state.query} /></span>
                </>
              );
              return (
                <li key={`${hit.session_id}:${hit.message_id ?? index}`}>
                  {onOpenSession ? (
                    <button type="button" className="kn-session-hit" onClick={() => onOpenSession(hit.session_id)}>
                      {body}
                    </button>
                  ) : (
                    <div className="kn-session-hit">{body}</div>
                  )}
                </li>
              );
            })}
          </ul>
        </>
      ))}
    </div>
  );
}

export default SessionSearch;
