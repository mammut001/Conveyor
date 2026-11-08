import { FormEvent, useCallback, useEffect, useRef, useState } from 'react';
import { formatDateTime, formatWhen } from '../ux/knowledgeKit';

// The agent's own notes (/api/memory/notes), shared by 记忆 (scope "global") and the
// 项目 page's 「项目笔记」 card (scope "repo:<repo_id>"). Every word the owner sees is
// plain Chinese: server errors arrive in Chinese with a `code`; anything else becomes
// one short Chinese sentence.

export type AgentNoteSource = 'agent' | 'owner' | 'untrusted';

export type AgentNote = {
  id: number;
  scope: string;
  text: string;
  source: AgentNoteSource | string;
  created_at: string;
  updated_at: string;
};

type NotesState = { notes: AgentNote[]; used: number; cap: number };

/** One note at most this many characters (agent_memory.NOTE_MAX_CHARS). */
export const NOTE_MAX_CHARS = 500;
const DEFAULT_CAP = 2200;

const SOURCE_LABEL: Record<AgentNoteSource, string> = {
  agent: '它记的',
  owner: '你写的',
  untrusted: '来自外部内容（仅供参考）',
};

const HAS_CJK = /[㐀-鿿]/;

const CODE_MESSAGES: Record<string, string> = {
  notes_full: '笔记已经满了，先删掉或改写旧的再记',
  looks_secret: '笔记里像是有密码或密钥，不能记',
  too_long: `每条笔记最多 ${NOTE_MAX_CHARS} 个字`,
  empty: '笔记不能是空的',
  not_one_paragraph: '每条笔记只写一段话，不要分段',
  duplicate: '已经有一条一模一样的笔记了',
  not_found: '找不到这条笔记，可能已经删掉了',
  invalid_scope: '笔记范围不正确，请刷新页面后再试',
  invalid_id: '笔记编号不正确，请刷新页面后再试',
  storage_unavailable: '笔记暂时读写不了，请稍后再试',
};

class NotesRequestError extends Error {
  status: number;
  code: string;
  constructor(message: string, status: number, code: string) {
    super(message);
    this.name = 'NotesRequestError';
    this.status = status;
    this.code = code;
  }
}

/** A Chinese sentence for a failed request; raw English never reaches the owner. */
export function notesErrorMessage(err: unknown, fallback: string): string {
  if (err instanceof NotesRequestError) {
    if (err.message && HAS_CJK.test(err.message)) return err.message;
    if (err.code && CODE_MESSAGES[err.code]) return CODE_MESSAGES[err.code];
    if (err.status === 401) return '登录已失效，请重新解锁控制台';
    if (err.status >= 500) return '服务器出错了，请稍后重试';
    return fallback;
  }
  if (err instanceof TypeError) return '网络连接失败，请检查网络后重试';
  return fallback;
}

async function notesRequest<T>(token: string, path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}`, ...(init?.headers || {}) },
  });
  const body = await res.json().catch(() => null) as Record<string, unknown> | null;
  if (!res.ok) {
    const error = body && typeof body.error === 'string' ? body.error : '';
    const code = body && typeof body.code === 'string' ? body.code : '';
    throw new NotesRequestError(error, res.status, code);
  }
  return body as T;
}

function noteSourceLabel(source: string): string {
  return SOURCE_LABEL[source as AgentNoteSource] || SOURCE_LABEL.agent;
}

/** Newest first: created time, then id. */
function newestFirst(a: AgentNote, b: AgentNote): number {
  const byTime = String(b.created_at || '').localeCompare(String(a.created_at || ''));
  return byTime !== 0 ? byTime : b.id - a.id;
}

/** Notes are one paragraph; a pasted line break becomes a space. */
function oneParagraph(text: string): string {
  return text.replace(/\s*[\r\n]+\s*/g, ' ');
}

export type NotesEditorProps = {
  token: string;
  /** "global" or "repo:<repo_id>". */
  scope: string;
  /** Short accessible name of the list, e.g. 「它自己记下的笔记」. */
  label: string;
  /** One muted line under the title explaining when the notes are used. */
  explain: string;
  /** Placeholder of the add box. */
  placeholder?: string;
  /** Reports how many notes the scope has, once loaded (for a collapsed summary). */
  onCountChange?: (count: number) => void;
};

export function NotesEditor({ token, scope, label, explain, placeholder = '例如：回复时先说结论', onCountChange }: NotesEditorProps) {
  const [data, setData] = useState<NotesState | null>(null);
  const [loadError, setLoadError] = useState('');
  const [disabled, setDisabled] = useState(false);
  const [missing, setMissing] = useState(false);
  const [newText, setNewText] = useState('');
  const [adding, setAdding] = useState(false);
  const [addError, setAddError] = useState('');
  const [savedNote, setSavedNote] = useState('');
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editingText, setEditingText] = useState('');
  const [editBusy, setEditBusy] = useState(false);
  const [editError, setEditError] = useState('');
  const [confirmId, setConfirmId] = useState<number | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState('');
  const fetchGen = useRef(0);
  const countRef = useRef(onCountChange);
  countRef.current = onCountChange;

  const load = useCallback(async () => {
    if (!token) return;
    const gen = ++fetchGen.current;
    try {
      const body = await notesRequest<Record<string, unknown>>(token, `/api/memory/notes?scope=${encodeURIComponent(scope)}`);
      if (gen !== fetchGen.current) return;
      if (!body || typeof body !== 'object' || !Array.isArray(body.notes)) throw new NotesRequestError('', 200, 'bad_shape');
      const notes = (body.notes as unknown[]).filter((note): note is AgentNote =>
        !!note && typeof note === 'object' && typeof (note as AgentNote).text === 'string' && typeof (note as AgentNote).id === 'number');
      const used = Number(body.used_chars);
      const cap = Number(body.cap_chars);
      setData({
        notes: [...notes].sort(newestFirst),
        used: Number.isFinite(used) && used >= 0 ? used : notes.reduce((sum, note) => sum + note.text.length, 0),
        cap: Number.isFinite(cap) && cap > 0 ? cap : DEFAULT_CAP,
      });
      setLoadError('');
      countRef.current?.(notes.length);
    } catch (err) {
      if (gen !== fetchGen.current) return;
      if (err instanceof NotesRequestError && err.status === 409) { setDisabled(true); return; }
      if (err instanceof NotesRequestError && err.status === 404 && !err.code) { setMissing(true); return; }
      setLoadError(notesErrorMessage(err, '没能读取笔记，请稍后重试'));
    }
  }, [token, scope]);

  // A different scope (another repository) starts from a clean slate.
  useEffect(() => {
    setData(null);
    setLoadError('');
    setDisabled(false);
    setMissing(false);
    setNewText('');
    setAddError('');
    setEditingId(null);
    setConfirmId(null);
    setDeleteError('');
    void load();
  }, [load]);

  useEffect(() => {
    if (!savedNote) return;
    const timer = window.setTimeout(() => setSavedNote(''), 4000);
    return () => window.clearTimeout(timer);
  }, [savedNote]);

  const handleAdd = async (event?: FormEvent) => {
    event?.preventDefault();
    const text = newText.trim();
    if (!text || adding || text.length > NOTE_MAX_CHARS) return;
    setAdding(true);
    setAddError('');
    setSavedNote('');
    try {
      await notesRequest(token, '/api/memory/notes', { method: 'POST', body: JSON.stringify({ scope, text }) });
      setNewText('');
      setSavedNote('已记下。');
      await load();
    } catch (err) {
      setAddError(notesErrorMessage(err, '没能记下这条笔记，请稍后重试'));
    } finally {
      setAdding(false);
    }
  };

  const handleEdit = async (id: number) => {
    const text = editingText.trim();
    if (!text || editBusy || text.length > NOTE_MAX_CHARS) return;
    setEditBusy(true);
    setEditError('');
    try {
      await notesRequest(token, `/api/memory/notes/${id}`, { method: 'PUT', body: JSON.stringify({ text }) });
      setEditingId(null);
      await load();
    } catch (err) {
      setEditError(notesErrorMessage(err, '修改没有成功，请稍后重试'));
    } finally {
      setEditBusy(false);
    }
  };

  const handleDelete = async (id: number) => {
    setDeleting(true);
    setDeleteError('');
    try {
      await notesRequest(token, `/api/memory/notes/${id}`, { method: 'DELETE' }).catch((err) => {
        // Already gone counts as deleted.
        if (err instanceof NotesRequestError && err.status === 404) return null;
        throw err;
      });
      setConfirmId(null);
      await load();
    } catch (err) {
      setDeleteError(notesErrorMessage(err, '删除没有成功，请稍后重试'));
    } finally {
      setDeleting(false);
    }
  };

  if (missing) return null;

  if (disabled) {
    return (
      <div className="kn-notes">
        <p className="kn-notes-explain">{explain}</p>
        <p className="kn-off">笔记功能未开启。</p>
      </div>
    );
  }

  const used = data?.used ?? 0;
  const cap = data?.cap ?? DEFAULT_CAP;
  const ratio = cap > 0 ? Math.min(1, used / cap) : 0;
  const level = ratio >= 1 ? ' is-full' : ratio >= 0.85 ? ' is-near' : '';
  const isOver = newText.trim().length > NOTE_MAX_CHARS;
  const idBase = `kn-notes-${scope.replace(/[^A-Za-z0-9_-]/g, '-')}`;
  const notes = data?.notes ?? [];

  return (
    <div className="kn-notes">
      <p className="kn-notes-explain">{explain}</p>

      {data && (
        <div className={`kn-notes-usage${level}`}>
          <div
            className="kn-notes-bar"
            role="meter"
            aria-label="笔记用量"
            aria-valuemin={0}
            aria-valuemax={cap}
            aria-valuenow={Math.min(used, cap)}
            aria-valuetext={`已用 ${used} / ${cap} 字`}
          >
            <span style={{ width: `${Math.round(ratio * 100)}%` }} />
          </div>
          <span className="kn-notes-usage-text">已用 {used} / {cap} 字</span>
        </div>
      )}

      <form className="kn-notes-add" onSubmit={handleAdd}>
        <label className="sr-only" htmlFor={`${idBase}-new`}>写一条笔记</label>
        <div className="kn-notes-add-row">
          <textarea
            id={`${idBase}-new`}
            className="kn-input kn-notes-textarea"
            rows={2}
            value={newText}
            placeholder={placeholder}
            onChange={(e) => {
              setNewText(oneParagraph(e.target.value));
              if (addError) setAddError('');
            }}
            onKeyDown={(e) => {
              if (e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229) return;
              if (e.key === 'Enter') {
                e.preventDefault();
                if (!isOver) void handleAdd();
              }
            }}
            aria-describedby={`${idBase}-count`}
            disabled={!data}
          />
          <button type="submit" className="kn-btn kn-btn-primary" disabled={adding || !data || !newText.trim() || isOver}>
            {adding ? '保存中…' : '添加笔记'}
          </button>
        </div>
        <span id={`${idBase}-count`} className={`kn-counter${isOver ? ' is-over' : ''}`} role={isOver ? 'alert' : undefined}>
          每条一段话 · {newText.trim().length} / {NOTE_MAX_CHARS}
          {isOver && `（超出 ${newText.trim().length - NOTE_MAX_CHARS} 字）`}
        </span>
        {addError && <p className="kn-notes-error" role="alert">{addError}</p>}
        {savedNote && <p className="kn-saved" role="status">{savedNote}</p>}
      </form>

      {loadError ? (
        <div className="kn-notes-load-error" role="alert">
          <span>{loadError}</span>
          <button type="button" className="kn-btn kn-btn-small" onClick={() => void load()}>重试</button>
        </div>
      ) : !data ? (
        <p className="kn-loading">正在读取笔记…</p>
      ) : notes.length === 0 ? (
        <p className="kn-notes-empty">还没有笔记。它在对话里学到的经验会记在这里，你也可以自己写一条。</p>
      ) : (
        <ul className="kn-notes-list" aria-label={label}>
          {notes.map((note) => {
            const editing = editingId === note.id;
            const confirming = confirmId === note.id;
            const source = SOURCE_LABEL[note.source as AgentNoteSource] ? note.source : 'agent';
            const when = note.updated_at || note.created_at;
            const editOver = editingText.trim().length > NOTE_MAX_CHARS;
            return (
              <li key={note.id} className={`kn-note-item kn-note-${source}`}>
                {editing ? (
                  <div className="kn-note-edit">
                    <textarea
                      className="kn-input kn-notes-textarea"
                      rows={3}
                      value={editingText}
                      onChange={(e) => { setEditingText(oneParagraph(e.target.value)); if (editError) setEditError(''); }}
                      onKeyDown={(e) => {
                        if (e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229) return;
                        if (e.key === 'Escape') { setEditingId(null); setEditError(''); }
                        if (e.key === 'Enter') {
                          e.preventDefault();
                          if (!editOver) void handleEdit(note.id);
                        }
                      }}
                      aria-label="修改笔记内容"
                      autoFocus
                    />
                    <div className="kn-row-actions">
                      <span className={`kn-counter${editOver ? ' is-over' : ''}`}>{editingText.trim().length} / {NOTE_MAX_CHARS}</span>
                      <button type="button" className="kn-btn kn-btn-primary" disabled={editBusy || !editingText.trim() || editOver} onClick={() => void handleEdit(note.id)}>
                        {editBusy ? '保存中…' : '保存修改'}
                      </button>
                      <button type="button" className="kn-btn" disabled={editBusy} onClick={() => { setEditingId(null); setEditError(''); }}>取消</button>
                    </div>
                    {editError && <p className="kn-notes-error" role="alert">{editError}</p>}
                  </div>
                ) : (
                  <p className="kn-note-text">{note.text}</p>
                )}
                <div className={`kn-note-foot${confirming ? ' is-confirming' : ''}`}>
                  <div className="kn-meta">
                    <span className={`kn-tag kn-note-source kn-note-source-${source}`}>{noteSourceLabel(source)}</span>
                    {when && <time dateTime={when} title={formatDateTime(when)}>{formatWhen(when)}</time>}
                  </div>
                  {!editing && (
                    <div className="kn-row-actions">
                      {confirming ? (
                        <>
                          <span className="kn-confirm-text">删除后无法恢复</span>
                          <button type="button" className="kn-btn kn-btn-danger" disabled={deleting} onClick={() => void handleDelete(note.id)}>
                            {deleting ? '删除中…' : '确定删除'}
                          </button>
                          <button type="button" className="kn-btn" disabled={deleting} onClick={() => { setConfirmId(null); setDeleteError(''); }}>取消</button>
                        </>
                      ) : (
                        <>
                          <button
                            type="button"
                            className="kn-btn kn-btn-quiet"
                            aria-label={`修改这条笔记：${note.text.slice(0, 24)}`}
                            onClick={() => { setEditingId(note.id); setEditingText(note.text); setEditError(''); setConfirmId(null); }}
                          >
                            修改
                          </button>
                          <button
                            type="button"
                            className="kn-btn kn-btn-quiet kn-btn-danger-text"
                            aria-label={`删除这条笔记：${note.text.slice(0, 24)}`}
                            onClick={() => { setConfirmId(note.id); setDeleteError(''); setEditingId(null); }}
                          >
                            删除
                          </button>
                        </>
                      )}
                    </div>
                  )}
                </div>
                {confirming && deleteError && <p className="kn-notes-error" role="alert">{deleteError}</p>}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

export default NotesEditor;
