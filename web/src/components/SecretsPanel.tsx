import { useCallback, useEffect, useState } from 'react';

type Secret = { name: string; set: boolean; updated_at: string | null };

export function SecretsPanel({ token }: { token: string }) {
  const [items, setItems] = useState<Secret[]>([]);
  const [name, setName] = useState('');
  const [value, setValue] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const response = await fetch('/api/secrets', {
        headers: { Authorization: 'Bearer ' + token },
        cache: 'no-store',
      });
      if (!response.ok) throw new Error('Unable to list secrets');
      const body = await response.json() as { items: Secret[] };
      setItems(body.items || []);
      setError('');
    } catch {
      setError('Could not load secrets');
    }
  }, [token]);

  useEffect(() => { void load(); }, [load]);

  async function save(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!/^[A-Z][A-Z0-9_]{1,63}$/.test(name) || !value) {
      setError('Use a valid uppercase environment variable name and nonempty value.');
      return;
    }
    setBusy(true);
    setNotice('');
    try {
      const response = await fetch('/api/secrets', {
        method: 'POST',
        headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, value }),
      });
      if (!response.ok) throw new Error('Failed to save secret');
      setValue('');
      setName('');
      setNotice('Secret stored. Values cannot be viewed again.');
      await load();
    } catch {
      setError('Unable to save secret');
    } finally {
      setBusy(false);
    }
  }

  async function remove(secretName: string) {
    if (!window.confirm('Remove stored value for ' + secretName + '?')) return;
    setBusy(true);
    try {
      const response = await fetch('/api/secrets/' + encodeURIComponent(secretName), {
        method: 'DELETE',
        headers: { Authorization: 'Bearer ' + token },
      });
      if (!response.ok) throw new Error('Delete failed');
      setNotice('Stored secret removed.');
      await load();
    } catch {
      setError('Unable to delete secret');
    } finally {
      setBusy(false);
    }
  }

  return <section className="panel">
    <h3>Secrets</h3>
    <p>Stored values stay on this server. They are never displayed; only a selected model provider key may enter an isolated child process.</p>
    <form onSubmit={event => { void save(event); }} autoComplete="off">
      <label htmlFor="secret-name">Environment variable name</label>
      <input id="secret-name" aria-label="Secret name" value={name} onChange={e => setName(e.target.value)}
        placeholder="OPENAI_API_KEY" autoCapitalize="characters" maxLength={64} required />
      <label htmlFor="secret-value">Secret value</label>
      <input id="secret-value" aria-label="Secret value" type="password" value={value}
        onChange={e => setValue(e.target.value)} autoComplete="new-password" required />
      <div className="action-row"><button type="submit" disabled={busy}>Save secret</button></div>
    </form>
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    <h4>Stored names</h4>
    {items.length === 0 ? <p>No secrets stored.</p> : <ul>
      {items.map(item => <li key={item.name}>
        <code>{item.name}</code> — {item.set ? 'set' : 'unset'}
        {item.updated_at ? ' · Updated ' + new Date(item.updated_at).toLocaleString() : ''}
        {item.set && <button type="button" disabled={busy} onClick={() => { void remove(item.name); }}>Remove</button>}
      </li>)}
    </ul>}
  </section>;
}
