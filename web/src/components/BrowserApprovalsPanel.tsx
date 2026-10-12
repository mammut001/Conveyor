import { useCallback, useEffect, useState } from 'react'

type Grant = { id: string; kind: 'path'|'actions'|'query'; host: string; port: number; path: string; query_field: string|null; expires_at: number; approver: string; action_types: string[] }
type Event = { timestamp: number; host: string; method: string; outcome: string; reason: string }
const date = (s: number) => new Date(s * 1000).toLocaleString()

export function BrowserApprovalsPanel({ token }: { token: string }) {
  const [grants, setGrants] = useState<Grant[]>([])
  const [events, setEvents] = useState<Event[]>([])
  const [killed, setKilled] = useState(false)
  const [limit, setLimit] = useState(50)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const headers = { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' }

  const reload = useCallback(async () => {
    try {
      const [a,b] = await Promise.all([
        fetch('/api/browser/approvals', { headers, cache:'no-store' }),
        fetch('/api/browser/egress?limit=' + limit, { headers, cache:'no-store' }),
      ])
      if (!a.ok || !b.ok) throw new Error('Could not load browser records')
      const approvals = await a.json()
      const log = await b.json()
      setGrants(approvals.approvals || [])
      setKilled(Boolean(approvals.kill_switch))
      setEvents(log.requests || [])
      setError('')
    } catch (err) { setError(err instanceof Error ? err.message : 'Load failed') }
  }, [token,limit])

  useEffect(() => { void reload() }, [reload])

  const toggle = async () => {
    if (!killed && !window.confirm('Block all external browser tasks immediately?')) return
    setBusy(true)
    try {
      const res = await fetch('/api/browser/kill', {
        method:'POST', headers, body:JSON.stringify({enabled:!killed}),
      })
      if (!res.ok) throw new Error('Could not update kill switch')
      await reload()
    } catch (err) { setError(err instanceof Error ? err.message : 'Kill switch failed') }
    finally { setBusy(false) }
  }

  const revoke = async (g: Grant) => {
    if (!window.confirm('Revoke ' + g.kind + ' permission for ' + g.host + g.path + '?')) return
    setBusy(true)
    try {
      const res = await fetch('/api/browser/approvals/' + encodeURIComponent(g.id), {
        method:'DELETE', headers,
      })
      if (!res.ok) throw new Error('Revoke failed')
      await reload()
    } catch (err) { setError(err instanceof Error ? err.message : 'Revoke failed') }
    finally { setBusy(false) }
  }

  return <div style={{ padding:20, overflowY:'auto', height:'100%' }}>
    <header style={{ display:'flex', justifyContent:'space-between', gap:12, flexWrap:'wrap' }}>
      <div><p className="eyebrow">EXTERNAL BROWSER · OPERATOR CONTROL</p>
        <h3>Site approvals</h3>
        <p className="memory-secondary">Exact HTTPS GET scopes. Webpage text cannot approve access.</p></div>
      <div className="action-row">
        <button type="button" disabled={busy} onClick={() => void reload()}>Refresh</button>
        <button type="button" disabled={busy} className={killed ? 'primary' : 'danger'}
          onClick={() => void toggle()}>{killed ? 'Clear global kill switch' : 'Kill external browsing'}</button>
      </div>
    </header>
    {error && <p role="alert">{error}</p>}
    <p role="status">Global kill switch: <strong>{killed ? 'ACTIVE — requests denied' : 'Inactive'}</strong>.
      G1/G2 feature flags remain default-off.</p>
    <div style={{ overflowX:'auto' }}>
      <table style={{ width:'100%', textAlign:'left' }}>
        <thead><tr>{['Host','Path','Permission','Approver','Expires',''].map(h =>
          <th key={h} style={{padding:8}}>{h}</th>)}</tr></thead>
        <tbody>{grants.map(g => <tr key={g.id}>
          <td style={{padding:8}}>{g.host}{g.port === 443 ? '' : ':' + g.port}</td>
          <td style={{padding:8}}><code>{g.path}</code></td>
          <td style={{padding:8}}>{g.kind === 'query' ? 'GET search · field ' + g.query_field :
            g.kind === 'actions' ? 'G2: ' + g.action_types.join(', ') : 'GET path'}</td>
          <td style={{padding:8}}><code title="Pseudonymous operator fingerprint">{g.approver}</code></td>
          <td style={{padding:8}}>{date(g.expires_at)}</td>
          <td style={{padding:8}}><button type="button" disabled={busy}
            aria-label={'Revoke ' + g.kind + ' on ' + g.host + g.path}
            onClick={() => void revoke(g)}>Revoke</button></td>
        </tr>)}</tbody>
      </table>
      {!grants.length && <p>No active browser approvals.</p>}
    </div>
    <section style={{marginTop:32}}>
      <header style={{display:'flex',justifyContent:'space-between'}}>
        <div><p className="eyebrow">REDACTED EGRESS JOURNAL</p><h3>Recent requests</h3></div>
        <label>Show last <select value={limit} onChange={e => setLimit(Number(e.target.value))}>
          {[20,50,100,200].map(n => <option key={n} value={n}>{n}</option>)}
        </select></label>
      </header>
      <div style={{overflowX:'auto'}}>
        <table style={{width:'100%',textAlign:'left'}}>
          <thead><tr>{['Time','Host','Method','Outcome','Reason'].map(h =>
            <th key={h} style={{padding:8}}>{h}</th>)}</tr></thead>
          <tbody>{events.map((e,i) => <tr key={e.timestamp + '-' + i}>
            <td style={{padding:8}}>{date(e.timestamp)}</td>
            <td style={{padding:8}}>{e.host}</td>
            <td style={{padding:8}}>{e.method}</td>
            <td style={{padding:8}}>{e.outcome}</td>
            <td style={{padding:8}}><code>{e.reason}</code></td>
          </tr>)}</tbody>
        </table>
      </div>
      {!events.length && <p>No recorded requests.</p>}
      <p className="memory-secondary">Paths, queries, cookies, bodies and page text are not recorded.</p>
    </section>
  </div>
}
