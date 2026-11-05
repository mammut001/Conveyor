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

const REASONS: Record<string, string> = {
  sensitive_input: '输入敏感信息', payment: '付款', login: '登录', captcha: '验证码',
  identity_check: '身份验证', operator_requested: '你主动接手', other: '其他原因',
}

function labelReason(reason?: string) {
  return REASONS[reason || 'operator_requested'] || (reason || '').replaceAll('_', ' ')
}

const PHASES: Record<string, string> = {
  idle: '空闲', starting: '启动中', ready: '已就绪', closing: '正在关闭', stopped: '已停止',
  waiting_for_idle: '等助手停下手头操作', manual_transport: '手动连接', unmanaged_transport: '外部连接',
  error: '出错', cleanup_error: '清理出错', disabled: '未开启', disabled_cleanup_error: '清理出错', unknown: '未知',
}

function labelPhase(phase?: string | null) {
  return PHASES[phase || 'unknown'] || (phase || '').replaceAll('_', ' ')
}

// A remembered token lives in localStorage only (a new tab has an empty sessionStorage).
function consoleToken() {
  try { return sessionStorage.getItem('conveyor-token') || localStorage.getItem('conveyor-token') || '' } catch { return '' }
}

export function HumanTakeoverPanel() {
  const [target, setTarget] = useState<HTMLElement | null>(null)
  const [idleTarget, setIdleTarget] = useState<HTMLElement | null>(null)
  const [status, setStatus] = useState<TakeoverStatus | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  // Served by plain web_console.py (no takeover routes): stop polling entirely.
  const [unavailable, setUnavailable] = useState(false)
  const unavailableRef = useRef(false)

  const load = useCallback(async () => {
    if (unavailableRef.current) return null
    const token = consoleToken()
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
    if (!response.ok) throw new Error('无法读取人工接管状态' + (body.error ? `（${body.error}）` : ''))
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
    const token = consoleToken()
    if (!token) throw new Error('控制台已锁定')
    setBusy(true); setError('')
    try {
      const response = await fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify(body),
      })
      const result = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error('接手操作没有成功' + (result.error ? `（${result.error}）` : ''))
      setStatus(result as TakeoverStatus)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '接手操作没有成功')
    } finally {
      setBusy(false)
    }
  }, [])

  useEffect(() => {
    const findTarget = () => {
      setTarget(document.querySelector<HTMLElement>('.takeover-active-slot') || document.querySelector<HTMLElement>('.context-panel'))
      setIdleTarget(document.querySelector<HTMLElement>('.takeover-slot'))
    }
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
          setError(reason instanceof Error ? reason.message : '无法读取人工接管状态')
        }
      })
    }
    refresh()
    const intervalMs = status?.enabled === false ? 30_000 : 2_000
    const timer = window.setInterval(refresh, intervalMs)
    return () => { active = false; window.clearInterval(timer) }
  }, [load, status?.enabled, unavailable])

  if (unavailable || !status) return null

  // Disabled on the server: nothing to show the owner (no env-var instructions).
  if (status.enabled === false) return null

  const lease = status.takeover
  const route = status.transport?.url || status.transport?.local_url || ''
  const closing = Boolean(status.closing)
  const activate = () => {
    if (!lease || lease.state !== 'waiting_for_human') return
    void mutate('/api/takeover/activate', { session_id: lease.id })
  }

  const card = <section className={`context-section takeover-context${lease ? ' is-active' : ''}`} aria-label="人工接管">
    <h3 className="eyebrow">人工接管</h3>
    {!lease ? <>
      <div className="takeover-idle">
        <span className="takeover-lock" aria-hidden="true">◇</span>
        <div><strong>助手正在使用电脑</strong><p>遇到密码、付款、验证码、身份验证这类需要你亲自操作的步骤，先暂停助手，由你接手。</p></div>
      </div>
      <button
        type="button"
        className="takeover-primary"
        disabled={busy}
        onClick={() => void mutate('/api/takeover/start', { reason: 'operator_requested', ttl_seconds: 300 })}
      >
        {busy ? '正在准备安全接手…' : '我来接手'}
      </button>
    </> : <>
      <div className="takeover-active-heading">
        <span className="takeover-lock active" aria-hidden="true">●</span>
        <div><strong>隐私模式 · 助手已暂停</strong><p>{labelReason(lease.reason)} · {remaining(lease.remaining_seconds)} 后自动结束</p></div>
      </div>
      <div className="takeover-status-grid">
        <span>接手状态</span><strong>{lease.state === 'human_active' ? '你正在操作' : '等你接手'}</strong>
        <span>远程桌面</span><strong>{closing ? '正在安全关闭…' : status.transport?.ready ? '已就绪' : labelPhase(status.transport?.phase)}</strong>
      </div>
      {status.transport?.error && <p className="takeover-error" role="alert">远程桌面出错<small>{status.transport.error}</small></p>}
      <div className="takeover-actions">
        {route && status.transport?.ready && !closing
          ? <a className="takeover-open" href={route} target="_blank" rel="noreferrer" onClick={activate}>打开远程桌面 ↗</a>
          : <button type="button" disabled>{status.transport?.phase === 'starting' ? '远程桌面启动中…' : '远程桌面还没就绪'}</button>}
        <button
          type="button"
          className="takeover-primary"
          disabled={busy || closing}
          onClick={() => void mutate('/api/takeover/complete', { session_id: lease.id })}
        >完成，交还助手</button>
        <button
          type="button"
          className="takeover-cancel"
          disabled={busy || closing}
          onClick={() => void mutate('/api/takeover/cancel', { session_id: lease.id })}
        >取消接手</button>
      </div>
      <p className="takeover-secret-note">敏感信息只留在远程桌面里。Conveyor 收不到远程桌面密码、账户密码、银行卡信息，也看不到你输入的任何机密。</p>
    </>}
    {error && <p className="takeover-error" role="alert">{error}</p>}
  </section>

  return <>
    {status.privacy_mode && <div className="takeover-privacy-banner" role="status">
      <strong>隐私模式</strong><span>人工接管期间，助手暂停操作电脑。</span>
      {status.closing && <em>正在关闭远程桌面，随后交还助手…</em>}
    </div>}
    {lease ? target && createPortal(card, target) : idleTarget && createPortal(card, idleTarget)}
  </>
}
