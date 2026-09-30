import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

type TakeoverLease = {
  id: string
  state: 'waiting_for_human' | 'human_active'
  reason: string
  task_id?: string | null
  remaining_seconds: number
}

type TakeoverStatus = {
  enabled?: boolean
  takeover: TakeoverLease | null
  privacy_mode: boolean
  closing: 'complete' | 'cancel' | null
  transport_allowed?: boolean
  message?: string | null
  transport: {
    phase: string
    running: boolean
    ready: boolean
    url?: string | null
    local_url?: string | null
    error?: string | null
    updated_at?: number
  } | null
}

function remaining(seconds: number) {
  const value = Math.max(0, Math.floor(seconds || 0))
  return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, '0')}`
}

function labelReason(reason?: string) {
  return (reason || 'operator requested').replaceAll('_', ' ')
}

export function HumanTakeoverPanel() {
  const [target, setTarget] = useState<HTMLElement | null>(null)
  const [status, setStatus] = useState<TakeoverStatus | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  // Served by plain web_console.py (no takeover routes): stop polling entirely.
  const [unavailable, setUnavailable] = useState(false)
  const unavailableRef = useRef(false)

  const load = useCallback(async () => {
    if (unavailableRef.current) return null
    const token = sessionStorage.getItem('conveyor-token') || ''
    if (!token) { setStatus(null); return null }
    const response = await fetch('/api/takeover/status', {
      headers: { Authorization: `Bearer ${token}` },
    })
    if (response.status === 404) {
      unavailableRef.current = true
      setUnavailable(true)
      setStatus(null)
      return null
    }
    if (response.status === 401) {
      setStatus(null)
      return null
    }
    const body = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(body.error || `Takeover status failed (${response.status})`)
    if (body && body.available === false) {
      unavailableRef.current = true
      setUnavailable(true)
      setStatus(null)
      return null
    }
    setStatus(body as TakeoverStatus)
    return body as TakeoverStatus
  }, [])

  const mutate = useCallback(async (path: string, body: Record<string, unknown>) => {
    const token = sessionStorage.getItem('conveyor-token') || ''
    if (!token) throw new Error('Web Console is locked')
    setBusy(true); setError('')
    try {
      const response = await fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify(body),
      })
      const result = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(result.error || `Takeover action failed (${response.status})`)
      setStatus(result as TakeoverStatus)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Takeover action failed')
    } finally {
      setBusy(false)
    }
  }, [])

  useEffect(() => {
    const findTarget = () => setTarget(document.querySelector<HTMLElement>('.context-panel'))
    findTarget()
    const observer = new MutationObserver(findTarget)
    observer.observe(document.body, { childList: true, subtree: true })
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    if (unavailable || unavailableRef.current) return
    let active = true
    const refresh = () => {
      if (unavailableRef.current) return
      void load().catch(reason => {
        if (active && !unavailableRef.current) {
          setError(reason instanceof Error ? reason.message : 'Could not load takeover status')
        }
      })
    }
    refresh()
    const intervalMs = status?.enabled === false ? 30_000 : 2_000
    const timer = window.setInterval(refresh, intervalMs)
    return () => { active = false; window.clearInterval(timer) }
  }, [load, status?.enabled, unavailable])

  if (unavailable || !target || !status) return null

  if (status.enabled === false) {
    const disabledCard = (
      <section className="context-section takeover-context" aria-label="Human takeover">
        <h3 className="eyebrow">HUMAN TAKEOVER</h3>
        <div className="takeover-idle">
          <span className="takeover-lock" aria-hidden="true">◇</span>
          <div>
            <p>Human takeover disabled — set CONVEYOR_TAKEOVER_ENABLED=true on the server</p>
          </div>
        </div>
      </section>
    )
    return createPortal(disabledCard, target)
  }

  const lease = status.takeover
  const route = status.transport?.url || status.transport?.local_url || ''
  const closing = Boolean(status.closing)
  const activate = () => {
    if (!lease || lease.state !== 'waiting_for_human') return
    void mutate('/api/takeover/activate', { session_id: lease.id })
  }

  const card = <section className="context-section takeover-context" aria-label="Human takeover">
    <h3 className="eyebrow">HUMAN TAKEOVER</h3>
    {!lease ? <>
      <div className="takeover-idle">
        <span className="takeover-lock" aria-hidden="true">◇</span>
        <div><strong>Agent owns the GUI</strong><p>Pause automation before passwords, payment, CAPTCHA, identity checks, or other human-only steps.</p></div>
      </div>
      <button
        type="button"
        className="takeover-primary"
        disabled={busy}
        onClick={() => void mutate('/api/takeover/start', { reason: 'operator_requested', ttl_seconds: 300 })}
      >
        {busy ? 'Opening secure handoff…' : 'Take over'}
      </button>
    </> : <>
      <div className="takeover-active-heading">
        <span className="takeover-lock active" aria-hidden="true">●</span>
        <div><strong>Privacy mode — Agent paused</strong><p>{labelReason(lease.reason)} · expires in {remaining(lease.remaining_seconds)}</p></div>
      </div>
      <div className="takeover-status-grid">
        <span>Lease</span><strong>{lease.state === 'human_active' ? 'Human active' : 'Waiting for human'}</strong>
        <span>Transport</span><strong>{closing ? 'Closing safely…' : status.transport?.ready ? 'Ready' : (status.transport?.phase || 'unknown').replaceAll('_', ' ')}</strong>
      </div>
      {status.transport?.error && <p className="takeover-error" role="alert">{status.transport.error}</p>}
      <div className="takeover-actions">
        {route && status.transport?.ready && !closing
          ? <a className="takeover-open" href={route} target="_blank" rel="noreferrer" onClick={activate}>Open Remote Desktop ↗</a>
          : <button type="button" disabled>Remote desktop {status.transport?.phase === 'starting' ? 'starting…' : 'not ready'}</button>}
        <button
          type="button"
          className="takeover-primary"
          disabled={busy || closing}
          onClick={() => void mutate('/api/takeover/complete', { session_id: lease.id })}
        >Done, resume Agent</button>
        <button
          type="button"
          className="takeover-cancel"
          disabled={busy || closing}
          onClick={() => void mutate('/api/takeover/cancel', { session_id: lease.id })}
        >Cancel handoff</button>
      </div>
      <p className="takeover-secret-note">Sensitive values stay in the remote desktop. Conveyor never receives the VNC credential, password, card data, or typed secret.</p>
    </>}
    {error && <p className="takeover-error" role="alert">{error}</p>}
  </section>

  return <>
    {status.privacy_mode && <div className="takeover-privacy-banner" role="status">
      <strong>Privacy mode</strong><span>Agent GUI automation is paused while human takeover is open.</span>
      {status.closing && <em>Closing transport before Agent resume…</em>}
    </div>}
    {createPortal(card, target)}
  </>
}
