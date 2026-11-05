import { useCallback, useEffect, useRef, useState, type ChangeEvent, type FormEvent } from 'react';

type Secret = { name: string; set: boolean; updated_at: string | null };
type Problem = { message: string; detail?: string };

const NAME_PATTERN = /^[A-Z][A-Z0-9_]{1,63}$/;
const MAX_VALUE_BYTES = 8192;
const BROWSER_LOGIN_PREFIX = 'BROWSER_LOGIN_';

const pad = (value: number) => String(value).padStart(2, '0');

/** "11月3日 14:05" (with the year only when it is not this year). */
function formatTime(iso: string | null): string {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  const year = date.getFullYear() === new Date().getFullYear() ? '' : `${date.getFullYear()}年`;
  return `${year}${date.getMonth() + 1}月${date.getDate()}日 ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** Server error codes are short English strings; never show them as the main message. */
function describeServerError(code: string | undefined, fallback: string): Problem {
  switch (code) {
    case 'invalid secret name':
      return { message: '名称格式不正确：只能用大写字母、数字和下划线，并以字母开头。' };
    case 'invalid secret value':
      return { message: '值不能为空，也不能超过 8 KB。' };
    case 'name and value required':
      return { message: '请同时填写名称和值。' };
    case 'not found':
      return { message: '这个密钥已经不存在了，列表已刷新。' };
    case 'unsafe secrets file permissions':
    case 'unsafe secrets path':
    case 'unsafe secrets directory':
      return { message: '服务器上的密钥文件权限不安全，已停止读写。', detail: code };
    case 'invalid secrets file':
      return { message: '服务器上的密钥文件已损坏，无法读取。', detail: code };
    default:
      return { message: fallback, detail: code };
  }
}

async function readError(response: Response): Promise<string> {
  const body = await response.json().catch(() => null) as { error?: unknown } | null;
  return typeof body?.error === 'string' ? body.error : `HTTP ${response.status}`;
}

export function SecretsPanel({ token }: { token: string }) {
  const [items, setItems] = useState<Secret[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [loadProblem, setLoadProblem] = useState<Problem | null>(null);
  const [name, setName] = useState('');
  const [value, setValue] = useState('');
  const [showValue, setShowValue] = useState(false);
  const [nameError, setNameError] = useState('');
  const [valueError, setValueError] = useState('');
  const [saveState, setSaveState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle');
  const [saveProblem, setSaveProblem] = useState<Problem | null>(null);
  const [savedName, setSavedName] = useState('');
  const [confirming, setConfirming] = useState('');
  const [deleting, setDeleting] = useState('');
  const [listNotice, setListNotice] = useState('');
  const [listProblem, setListProblem] = useState<Problem | null>(null);
  const formRef = useRef<HTMLFormElement>(null);
  const nameRef = useRef<HTMLInputElement>(null);
  const valueRef = useRef<HTMLInputElement>(null);
  const deleteButtons = useRef(new Map<string, HTMLButtonElement>());

  const load = useCallback(async () => {
    try {
      const response = await fetch('/api/secrets', {
        headers: { Authorization: 'Bearer ' + token },
        cache: 'no-store',
      });
      if (!response.ok) {
        setLoadProblem(describeServerError(await readError(response), '无法读取密钥列表，请稍后重试。'));
        return;
      }
      const body = await response.json() as { items?: Secret[] };
      setItems(Array.isArray(body.items) ? body.items : []);
      setLoadProblem(null);
    } catch {
      setLoadProblem({ message: '无法连接服务器，密钥列表暂时读取不到。' });
    } finally {
      setLoaded(true);
    }
  }, [token]);

  useEffect(() => { void load(); }, [load]);

  function editing() {
    if (saveState === 'saved' || saveState === 'error') {
      setSaveState('idle');
      setSaveProblem(null);
    }
  }

  function changeName(event: ChangeEvent<HTMLInputElement>) {
    const input = event.currentTarget;
    const caret = input.selectionStart;
    const next = input.value.toUpperCase();
    setName(next);
    setNameError('');
    editing();
    // Upper-casing rewrites the value; keep the caret where the user was typing.
    if (next !== input.value && caret !== null) requestAnimationFrame(() => input.setSelectionRange(caret, caret));
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmedName = name.trim();
    const badName = !trimmedName
      ? '请填写名称。'
      : NAME_PATTERN.test(trimmedName) ? '' : '只能用大写字母、数字和下划线，以字母开头，至少 2 个字符。';
    const badValue = !value
      ? '请填写值。'
      : new TextEncoder().encode(value).length > MAX_VALUE_BYTES ? '值太长了，不能超过 8 KB。' : '';
    setNameError(badName);
    setValueError(badValue);
    if (badName || badValue) {
      (badName ? nameRef : valueRef).current?.focus();
      return;
    }
    setSaveState('saving');
    setSaveProblem(null);
    setListNotice('');
    try {
      const response = await fetch('/api/secrets', {
        method: 'POST',
        headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: trimmedName, value }),
      });
      if (!response.ok) {
        setSaveProblem(describeServerError(await readError(response), '保存失败，请稍后重试。'));
        setSaveState('error');
        return;
      }
      setValue('');
      setName('');
      setShowValue(false);
      setSavedName(trimmedName);
      setSaveState('saved');
      await load();
    } catch {
      setSaveProblem({ message: '无法连接服务器，密钥没有保存。' });
      setSaveState('error');
    }
  }

  function replace(secretName: string) {
    setName(secretName);
    setNameError('');
    setValue('');
    setValueError('');
    editing();
    formRef.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    valueRef.current?.focus({ preventScroll: true });
  }

  function cancelConfirm(secretName: string) {
    setConfirming('');
    requestAnimationFrame(() => deleteButtons.current.get(secretName)?.focus());
  }

  async function remove(secretName: string) {
    setDeleting(secretName);
    setListNotice('');
    setListProblem(null);
    try {
      const response = await fetch('/api/secrets/' + encodeURIComponent(secretName), {
        method: 'DELETE',
        headers: { Authorization: 'Bearer ' + token },
      });
      if (!response.ok) {
        setListProblem(describeServerError(await readError(response), `删除 ${secretName} 失败，请稍后重试。`));
      } else {
        setListNotice(`已删除 ${secretName}。`);
      }
      setConfirming('');
      await load();
    } catch {
      setListProblem({ message: '无法连接服务器，密钥没有删除。' });
    } finally {
      setDeleting('');
    }
  }

  const stored = items.filter(item => item.set);
  const ordered = [...stored, ...items.filter(item => !item.set)];
  const saving = saveState === 'saving';

  return <div className="guard-page guard-secrets">
    <div className="guard-stack">
      <p className="guard-intro">
        密钥只保存在这台服务器上，保存后不会再显示。只有你选用的模型服务会在单独的隔离进程里用到它。
      </p>

      <section className="guard-card" aria-labelledby="guard-secrets-form-title">
        <h3 id="guard-secrets-form-title" className="guard-card-title">保存密钥</h3>
        <form ref={formRef} className="guard-form" onSubmit={event => { void save(event); }} autoComplete="off" noValidate>
          <div className="guard-field">
            <label htmlFor="secret-name">名称</label>
            <input
              ref={nameRef}
              id="secret-name"
              className="guard-input guard-input-mono"
              value={name}
              onChange={changeName}
              placeholder="例如 DEEPSEEK_API_KEY"
              autoCapitalize="characters"
              autoCorrect="off"
              spellCheck={false}
              maxLength={64}
              aria-invalid={nameError ? true : undefined}
              aria-describedby={nameError ? 'secret-name-error' : 'secret-name-hint'}
            />
            {nameError
              ? <p id="secret-name-error" className="guard-field-error">{nameError}</p>
              : <p id="secret-name-hint" className="guard-hint">填模型服务要求的名称。只能用大写字母、数字和下划线；同名会覆盖原来的值。</p>}
          </div>
          <div className="guard-field">
            <label htmlFor="secret-value">值</label>
            <div className="guard-input-group">
              <input
                ref={valueRef}
                id="secret-value"
                className="guard-input guard-input-mono"
                type={showValue ? 'text' : 'password'}
                value={value}
                onChange={event => { setValue(event.target.value); setValueError(''); editing(); }}
                placeholder="粘贴密钥的值"
                autoComplete="new-password"
                autoCorrect="off"
                autoCapitalize="off"
                spellCheck={false}
                aria-invalid={valueError ? true : undefined}
                aria-describedby={valueError ? 'secret-value-error' : undefined}
              />
              <button
                type="button"
                className="guard-btn guard-input-toggle"
                aria-controls="secret-value"
                aria-label={showValue ? '隐藏值' : '显示值'}
                onClick={() => setShowValue(current => !current)}
              >{showValue ? '隐藏' : '显示'}</button>
            </div>
            {valueError && <p id="secret-value-error" className="guard-field-error">{valueError}</p>}
          </div>
          <div className="guard-form-footer">
            <button type="submit" className="guard-btn guard-btn-primary" disabled={saving} aria-busy={saving || undefined}>
              {saving ? '保存中…' : '保存'}
            </button>
            <div className="guard-form-feedback" aria-live="polite">
              {saveState === 'saved' && <p role="status" className="guard-feedback guard-feedback-ok">
                已保存 <code>{savedName}</code>。出于安全考虑，值不会再显示。
              </p>}
              {saveState === 'error' && saveProblem && <p role="alert" className="guard-feedback guard-feedback-error">
                {saveProblem.message}
                {saveProblem.detail && <span className="guard-tech">{saveProblem.detail}</span>}
              </p>}
            </div>
          </div>
        </form>
      </section>

      <section className="guard-card" aria-labelledby="guard-secrets-list-title">
        <div className="guard-card-head">
          <h3 id="guard-secrets-list-title" className="guard-card-title">已保存的密钥</h3>
          {loaded && !loadProblem && stored.length > 0 && <span className="guard-count">{stored.length} 个</span>}
        </div>
        {(listNotice || listProblem) && <div aria-live="polite">
          {listNotice && <p role="status" className="guard-feedback guard-feedback-ok">{listNotice}</p>}
          {listProblem && <p role="alert" className="guard-feedback guard-feedback-error">
            {listProblem.message}
            {listProblem.detail && <span className="guard-tech">{listProblem.detail}</span>}
          </p>}
        </div>}
        {!loaded ? <p className="guard-empty">加载中…</p>
          : loadProblem ? <div className="guard-empty" role="alert">
            <p>{loadProblem.message}</p>
            {loadProblem.detail && <p className="guard-tech">{loadProblem.detail}</p>}
            <button type="button" className="guard-btn" onClick={() => { void load(); }}>重试</button>
          </div>
          : ordered.length === 0 ? <p className="guard-empty">还没有保存任何密钥</p>
          : <ul className="guard-secret-list">
            {ordered.map(item => {
              const when = formatTime(item.updated_at);
              const isConfirming = confirming === item.name;
              return <li key={item.name} className={`guard-secret-row${item.set ? '' : ' is-removed'}`}>
                <div className="guard-secret-main">
                  <code className="guard-secret-name">{item.name}</code>
                  {item.set
                    ? <span className="guard-badge guard-badge-ok">已保存</span>
                    : <span className="guard-badge guard-badge-muted">已删除</span>}
                  {item.name.startsWith(BROWSER_LOGIN_PREFIX) && <span className="guard-badge guard-badge-muted">网站登录</span>}
                </div>
                {when && <span className="guard-secret-time">{when} {item.set ? '更新' : '删除'}</span>}
                {!isConfirming && <div className="guard-row-actions">
                  <button type="button" className="guard-btn guard-btn-small" disabled={Boolean(deleting)}
                    aria-label={`${item.set ? '更换' : '重新填写'} ${item.name}`}
                    onClick={() => replace(item.name)}>{item.set ? '更换' : '重新填写'}</button>
                  {item.set && <button type="button" className="guard-btn guard-btn-small guard-btn-danger"
                    ref={element => { if (element) deleteButtons.current.set(item.name, element); else deleteButtons.current.delete(item.name); }}
                    disabled={Boolean(deleting)} aria-label={`删除 ${item.name}`}
                    onClick={() => { setConfirming(item.name); setListNotice(''); setListProblem(null); }}>删除</button>}
                </div>}
                {isConfirming && <div className="guard-inline-confirm" role="group" aria-label={`确认删除 ${item.name}`}
                  onKeyDown={event => { if (event.key === 'Escape') cancelConfirm(item.name); }}>
                  <p>删除后，服务器会清除这个值，用到它的模型服务将无法调用，直到你重新保存。</p>
                  <div className="guard-inline-confirm-actions">
                    <button type="button" className="guard-btn guard-btn-small" autoFocus disabled={deleting === item.name}
                      onClick={() => cancelConfirm(item.name)}>取消</button>
                    <button type="button" className="guard-btn guard-btn-small guard-btn-danger-solid" disabled={deleting === item.name}
                      onClick={() => { void remove(item.name); }}>{deleting === item.name ? '删除中…' : '确认删除'}</button>
                  </div>
                </div>}
              </li>;
            })}
          </ul>}
      </section>
    </div>
  </div>;
}
