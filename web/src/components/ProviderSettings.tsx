import { useEffect, useState } from 'react'
import type { ProviderConfig } from '../types'

export function ProviderSettings({ config, busy, onClose, onSave }: {
  config: ProviderConfig | null
  busy: boolean
  onClose: () => void
  onSave: (payload: Record<string, string>) => Promise<void>
}) {
  const [draft, setDraft] = useState({ provider_id: '', provider_name: '', model: '', reasoning_effort: 'minimal', base_url: '', wire_api: 'responses', env_key: 'OPENAI_API_KEY', api_key: '' })
  const [saved, setSaved] = useState(false)
  useEffect(() => { if (config) setDraft({ ...config, api_key: '' }) }, [config])
  function field(name: keyof typeof draft, value: string) { setSaved(false); setDraft(previous => ({ ...previous, [name]: value })) }
  const health = config?.health?.status

  return <div className="settings-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) onClose() }}>
    <section className="settings-sheet" role="dialog" aria-modal="true" aria-label="Provider settings">
      <header><div><p className="eyebrow">MODEL PROVIDER</p><h2>Configuration</h2><p>Provider settings used by new Conveyor tasks.</p></div><button className="close-button" onClick={onClose} aria-label="Close settings">×</button></header>
      {!config ? <div className="settings-loading">Loading configuration…</div> : <form onSubmit={event => { event.preventDefault(); void onSave(draft).then(() => { setSaved(true); setDraft(previous => ({ ...previous, api_key: '' })) }).catch(() => {}) }}>
        {health && <div className={`v3-provider-health ${health}`}><span className="v3-live-dot" /><strong>{health.replace('_', ' ')}</strong><span>{config.provider_name} · {config.model}</span></div>}
        <div className="form-grid">
          <label>Provider ID<input value={draft.provider_id} onChange={event => field('provider_id', event.target.value)} placeholder="deepseek" required /></label>
          <label>Display name<input value={draft.provider_name} onChange={event => field('provider_name', event.target.value)} placeholder="DeepSeek" required /></label>
          <label className="wide">Base URL<input value={draft.base_url} onChange={event => field('base_url', event.target.value)} placeholder="https://api.deepseek.com/v1" required /></label>
          <label>Model<input value={draft.model} onChange={event => field('model', event.target.value)} placeholder="deepseek-chat" required /></label>
          <label>API protocol<select value={draft.wire_api} onChange={event => field('wire_api', event.target.value)}><option value="responses">Responses</option><option value="chat">Chat Completions</option></select></label>
          <label>Reasoning<select value={draft.reasoning_effort} onChange={event => field('reasoning_effort', event.target.value)}>{['none','minimal','low','medium','high','xhigh'].map(value => <option key={value}>{value}</option>)}</select></label>
          <label>Key variable<input value={draft.env_key} onChange={event => field('env_key', event.target.value.toUpperCase())} placeholder="OPENAI_API_KEY" required /></label>
          <label className="wide">API key<input type="password" autoComplete="off" value={draft.api_key} onChange={event => field('api_key', event.target.value)} placeholder={config.api_key_configured ? `Configured ${config.api_key_hint} · leave blank to keep` : 'Paste a new API key'} /></label>
        </div>
        <div className="config-note"><strong>Saved securely on the VPS</strong><span>The browser never receives the full key. Changes apply to the next task and update <code>config.toml</code> plus the service <code>.env</code>.</span></div>
        <footer><span className={saved ? 'save-status visible' : 'save-status'}>✓ Saved. New tasks will use this provider.</span><button type="button" onClick={onClose}>Cancel</button><button className="primary" disabled={busy}>{busy ? 'Saving…' : 'Save configuration'}</button></footer>
      </form>}
    </section>
  </div>
}
