import { useCallback, useEffect, useRef, useState } from 'react'
import { pausedByLabel, screenMessage } from '../taskText'

type ScreenStatus = {
  enabled: boolean
  available: boolean
  reason: string
  control_enabled: boolean
  controlling: boolean
  agent_paused: boolean
  blocked_by: string | null
  dedicated?: boolean
  width: number
  height: number
}

type InputEvent =
  | { t: 'move'; x: number; y: number }
  | { t: 'down' | 'up'; x: number; y: number; b: number }
  | { t: 'scroll'; x: number; y: number; dir: 'up' | 'down' | 'left' | 'right'; n: number }
  | { t: 'key'; key: string; mods: string[] }
  | { t: 'text'; text: string }

const MODIFIER_KEYS = new Set(['Shift', 'Control', 'Alt', 'Meta', 'AltGraph', 'Fn', 'Dead', 'Process', 'Unidentified'])
const MAX_TEXT_CHARS = 500
const THUMBNAIL_PAUSE_MS = 900
const MOVE_INTERVAL_MS = 45
// A classic arrow, tip at the origin, in 1/1000-of-screen-width units.
const POINTER_SHAPE: [number, number][] = [[0, 0], [0, 17], [4.5, 13], [7.5, 20], [10.5, 18.7], [7.5, 12], [13, 12]]

/**
 * Live view of the host desktop with one-click takeover.
 *
 * Frames are fetched with the bearer token and painted onto canvases, so the
 * screen never becomes a URL the browser could cache or leak. Input is only
 * sent while the operator holds the control lease (the Agent is paused).
 */
export function LiveScreenPanel({ token, agentId = '', agentName = '' }: { token: string; agentId?: string; agentName?: string }) {
  const [status, setStatus] = useState<ScreenStatus | null>(null)
  const [expanded, setExpanded] = useState(false)
  const [hasFrame, setHasFrame] = useState(false)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [typed, setTyped] = useState('')

  const thumbRef = useRef<HTMLCanvasElement>(null)
  const viewRef = useRef<HTMLCanvasElement>(null)
  const bitmapRef = useRef<ImageBitmap | null>(null)
  const sizeRef = useRef({ width: 0, height: 0 })
  const pointerRef = useRef({ x: -1, y: -1 })
  const expandedRef = useRef(false)
  const controllingRef = useRef(false)
  const queueRef = useRef<InputEvent[]>([])
  const sendingRef = useRef(false)
  const lastMoveRef = useRef(0)
  const armedRef = useRef(false)

  const controlling = Boolean(status?.controlling)
  expandedRef.current = expanded
  controllingRef.current = controlling

  // Every call names the agent, so each agent's panel talks to its own desktop.
  const agentQuery = agentId ? `agent=${encodeURIComponent(agentId)}` : ''
  const request = useCallback(async <T,>(path: string, body?: Record<string, unknown>): Promise<T> => {
    const url = body === undefined && agentQuery ? `${path}?${agentQuery}` : path
    const response = await fetch(url, {
      method: body === undefined ? 'GET' : 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: body === undefined ? undefined : JSON.stringify(agentId ? { ...body, agent: agentId } : body),
    })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(typeof data.error === 'string' ? data.error : `Request failed (${response.status})`)
    return data as T
  }, [token, agentId, agentQuery])

  const paint = useCallback(() => {
    const bitmap = bitmapRef.current
    if (!bitmap) return
    for (const canvas of [thumbRef.current, viewRef.current]) {
      if (!canvas) continue
      if (canvas.width !== bitmap.width || canvas.height !== bitmap.height) {
        canvas.width = bitmap.width
        canvas.height = bitmap.height
      }
      const context = canvas.getContext('2d')
      if (!context) continue
      context.drawImage(bitmap, 0, 0)
      // Captures carry no cursor. While watching, draw where the host pointer
      // is; while in control the operator's own cursor already shows it.
      const { x, y } = pointerRef.current
      const { width, height } = sizeRef.current
      if (controllingRef.current || x < 0 || y < 0 || !width || !height) continue
      const unit = Math.max(1, bitmap.width / 1000)
      context.save()
      context.translate((x / width) * bitmap.width, (y / height) * bitmap.height)
      context.scale(unit, unit)
      context.beginPath()
      for (const [px, py] of POINTER_SHAPE) context.lineTo(px, py)
      context.closePath()
      context.fillStyle = '#fff'
      context.strokeStyle = '#000'
      context.lineWidth = 1.5
      context.lineJoin = 'round'
      context.fill()
      context.stroke()
      context.restore()
    }
  }, [])

  // Status: cheap, and tells us when the lease is lost server-side.
  useEffect(() => {
    let active = true
    const load = () => {
      request<ScreenStatus>('/api/screen/status')
        .then(value => { if (active) setStatus(value) })
        .catch(() => {})
    }
    load()
    const timer = window.setInterval(load, 4000)
    return () => { active = false; window.clearInterval(timer) }
  }, [request])

  // Frames: long-poll continuously while open, lazily for the thumbnail.
  const available = Boolean(status?.enabled && status.available)
  useEffect(() => {
    if (!available) return
    let active = true
    let seq = 0
    const controller = new AbortController()
    const sleep = (ms: number) => new Promise(resolve => window.setTimeout(resolve, ms))
    const loop = async () => {
      while (active) {
        if (document.hidden) { await sleep(1000); continue }
        try {
          const response = await fetch(`/api/screen/frame?since=${seq}${agentQuery ? `&${agentQuery}` : ''}`, {
            headers: { Authorization: `Bearer ${token}` }, signal: controller.signal,
          })
          if (response.status === 200) {
            seq = Number(response.headers.get('X-Frame-Seq')) || seq + 1
            const bitmap = await createImageBitmap(await response.blob())
            if (!active) { bitmap.close(); return }
            bitmapRef.current?.close()
            bitmapRef.current = bitmap
            sizeRef.current = {
              width: Number(response.headers.get('X-Screen-Width')) || bitmap.width,
              height: Number(response.headers.get('X-Screen-Height')) || bitmap.height,
            }
            pointerRef.current = {
              x: Number(response.headers.get('X-Pointer-X') ?? -1),
              y: Number(response.headers.get('X-Pointer-Y') ?? -1),
            }
            paint()
            setHasFrame(true)
            setError('')
          } else if (response.status !== 204) {
            const data = await response.json().catch(() => ({}))
            setError(screenMessage(typeof data.error === 'string' ? data.error : ''))
            await sleep(3000)
          }
        } catch {
          if (!active) return
          await sleep(2000)
        }
        if (!expandedRef.current) await sleep(THUMBNAIL_PAUSE_MS)
      }
    }
    void loop()
    return () => { active = false; controller.abort() }
  }, [available, token, paint, agentQuery])

  // A freshly mounted viewer canvas needs the current frame immediately.
  useEffect(() => { if (expanded) paint() }, [expanded, paint])
  useEffect(() => { paint() }, [controlling, paint])

  const flush = useCallback(async () => {
    if (sendingRef.current) return
    sendingRef.current = true
    try {
      while (queueRef.current.length && controllingRef.current) {
        const events = queueRef.current.splice(0, 48)
        try {
          await request('/api/screen/input', { events })
        } catch (reason) {
          queueRef.current = []
          setError(screenMessage(reason instanceof Error ? reason.message : '', '输入没有送达主机'))
          request<ScreenStatus>('/api/screen/status').then(setStatus).catch(() => {})
        }
      }
    } finally {
      sendingRef.current = false
    }
  }, [request])

  const send = useCallback((event: InputEvent) => {
    if (!controllingRef.current) return
    const queue = queueRef.current
    // Only the newest pointer position matters between two sends.
    if (event.t === 'move' && queue.length && queue[queue.length - 1].t === 'move') queue[queue.length - 1] = event
    else queue.push(event)
    void flush()
  }, [flush])

  const setControl = useCallback(async (action: 'take' | 'release') => {
    setBusy(true)
    try {
      queueRef.current = []
      armedRef.current = false
      setStatus(await request<ScreenStatus>('/api/screen/control', { action }))
      setError('')
      if (action === 'take') window.setTimeout(() => viewRef.current?.focus(), 0)
    } catch (reason) {
      setError(screenMessage(reason instanceof Error ? reason.message : '', '无法切换操作权'))
    } finally {
      setBusy(false)
    }
  }, [request])

  const close = useCallback(() => {
    if (controllingRef.current) void setControl('release')
    setExpanded(false)
  }, [setControl])

  // Never leave the Agent paused behind a closed tab.
  useEffect(() => {
    const release = () => {
      if (!controllingRef.current) return
      void fetch('/api/screen/control', {
        method: 'POST', keepalive: true,
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify(agentId ? { action: 'release', agent: agentId } : { action: 'release' }),
      })
    }
    window.addEventListener('pagehide', release)
    return () => { window.removeEventListener('pagehide', release); release() }
  }, [token, agentId])

  // An agent's own desktop starts empty: bring its browser up the first time
  // the screen is opened. Asking again while one is running does nothing.
  const openBrowser = useCallback(() => { request('/api/screen/launch', {}).catch(() => {}) }, [request])
  const dedicated = Boolean(status?.dedicated)
  useEffect(() => { if (expanded && dedicated) openBrowser() }, [expanded, dedicated, openBrowser])

  useEffect(() => {
    if (!expanded || controlling) return
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') close() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [expanded, controlling, close])

  const point = (event: { clientX: number; clientY: number }) => {
    const canvas = viewRef.current
    const { width, height } = sizeRef.current
    if (!canvas || !width || !height) return null
    const rect = canvas.getBoundingClientRect()
    if (!rect.width || !rect.height) return null
    // The canvas fills its frame with object-fit: contain, so the picture is
    // letterboxed inside the element; map through the drawn area, not the box.
    const scale = Math.min(rect.width / width, rect.height / height)
    const left = rect.left + (rect.width - width * scale) / 2
    const top = rect.top + (rect.height - height * scale) / 2
    const x = Math.round((event.clientX - left) / scale)
    const y = Math.round((event.clientY - top) / scale)
    return { x: Math.min(width - 1, Math.max(0, x)), y: Math.min(height - 1, Math.max(0, y)) }
  }

  const sendText = (text: string) => {
    for (let index = 0; index < text.length; index += MAX_TEXT_CHARS) {
      send({ t: 'text', text: text.slice(index, index + MAX_TEXT_CHARS) })
    }
  }

  if (!status?.enabled) return null

  const canTake = status.available && status.control_enabled && !controlling && !status.blocked_by

  const owner = dedicated && agentName ? `${agentName} 的桌面 · ` : ''
  const subtitle = !status.available ? screenMessage(status.reason)
    : controlling ? '你正在操作 · 助手已暂停'
    : status.blocked_by ? `${owner}助手已暂停（${pausedByLabel(status.blocked_by)}）`
    : `${owner}实时 · 仅查看`

  return (
    <div className="live-screen-card">
      <div className="host-screen-heading">
        <div><strong>实时画面</strong><small>{subtitle}</small></div>
        {controlling && <span className="live-screen-pill">你在操作</span>}
      </div>
      {status.available
        ? <button type="button" className="live-screen-thumb" onClick={() => setExpanded(true)} aria-label="放大实时画面">
            <canvas ref={thumbRef} aria-hidden="true" />
            {!hasFrame && <span className="live-screen-waiting">连接中…</span>}
            <span className="live-screen-open"><span aria-hidden="true">⤢</span> 放大</span>
          </button>
        : <div className="screen-empty"><span aria-hidden="true">▣</span><strong>{screenMessage(status.reason)}</strong>{status.reason && <p className="screen-empty-detail">{status.reason}</p>}</div>}
      {error && !expanded && <p className="screen-request-status failed" role="alert">{error}</p>}

      {expanded && <div className="screen-viewer-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget && !controlling) close() }}>
        <section className={`screen-viewer live-screen-viewer${controlling ? ' controlling' : ''}`} role="dialog" aria-modal="true" aria-labelledby="live-screen-title">
          <header className="screen-viewer-header">
            <div>
              <p className="eyebrow">{controlling ? '你正在操作 · 助手已暂停' : dedicated ? '助手桌面 · 实时画面' : '主机 · 实时画面'}</p>
              <h2 id="live-screen-title">{dedicated && agentName ? `${agentName} 的屏幕` : '主机屏幕'}</h2>
              <p>{sizeRef.current.width ? `${sizeRef.current.width} × ${sizeRef.current.height}` : '连接中…'}{status.blocked_by ? ` · ${pausedByLabel(status.blocked_by)}` : ''}</p>
            </div>
            <div className="live-screen-actions">
              {dedicated && <button type="button" className="live-screen-secondary" onClick={openBrowser}>打开浏览器</button>}
              {status.control_enabled && (controlling
                ? <button type="button" className="live-screen-release" disabled={busy} onClick={() => void setControl('release')}>交还给助手</button>
                : <button type="button" className="live-screen-take" disabled={busy || Boolean(status.blocked_by)} onClick={() => void setControl('take')}>{busy ? '正在接管…' : '接管操作'}</button>)}
              <button type="button" className="close-button" onClick={close} aria-label="关闭实时画面">×</button>
            </div>
          </header>
          <div className="screen-viewer-image-frame">
            <canvas
              ref={viewRef}
              className={`live-screen-canvas${canTake ? ' takeable' : ''}`}
              onClick={() => { if (canTake && !busy) void setControl('take') }}
              tabIndex={controlling ? 0 : -1}
              aria-label="实时画面"
              onContextMenu={event => event.preventDefault()}
              onPointerDown={event => {
                if (!controlling) return
                armedRef.current = true
                event.preventDefault()
                event.currentTarget.focus()
                event.currentTarget.setPointerCapture(event.pointerId)
                const at = point(event)
                if (at) send({ t: 'down', ...at, b: event.button === 2 ? 3 : event.button === 1 ? 2 : 1 })
              }}
              onPointerUp={event => {
                // `armedRef`: the click that takes control must not also land on the host.
                if (!controlling || !armedRef.current) return
                event.preventDefault()
                const at = point(event)
                if (at) send({ t: 'up', ...at, b: event.button === 2 ? 3 : event.button === 1 ? 2 : 1 })
              }}
              onPointerMove={event => {
                if (!controlling) return
                const now = performance.now()
                if (now - lastMoveRef.current < MOVE_INTERVAL_MS) return
                lastMoveRef.current = now
                const at = point(event)
                if (at) send({ t: 'move', ...at })
              }}
              onWheel={event => {
                if (!controlling) return
                const at = point(event)
                if (!at) return
                const vertical = Math.abs(event.deltaY) >= Math.abs(event.deltaX)
                const delta = vertical ? event.deltaY : event.deltaX
                if (!delta) return
                const dir = vertical ? (delta > 0 ? 'down' : 'up') : (delta > 0 ? 'right' : 'left')
                send({ t: 'scroll', ...at, dir, n: Math.min(5, Math.max(1, Math.round(Math.abs(delta) / 100))) })
              }}
              onKeyDown={event => {
                if (!controlling || MODIFIER_KEYS.has(event.key)) return
                event.preventDefault()
                const chord = event.ctrlKey || event.altKey || event.metaKey
                if (event.key.length === 1 && !chord) { send({ t: 'text', text: event.key }); return }
                const mods = [event.ctrlKey && 'ctrl', event.altKey && 'alt', event.shiftKey && 'shift', event.metaKey && 'meta'].filter(Boolean) as string[]
                send({ t: 'key', key: event.key, mods })
              }}
              onPaste={event => {
                if (!controlling) return
                event.preventDefault()
                sendText(event.clipboardData.getData('text'))
              }}
            />
            {canTake && hasFrame && <span className="live-screen-hint">{busy ? '正在接管…' : '点击画面即可接管操作'}</span>}
          </div>
          <footer className="screen-viewer-footer live-screen-footer">
            {controlling
              ? <form onSubmit={event => { event.preventDefault(); if (typed) sendText(typed); send({ t: 'key', key: 'Enter', mods: [] }); setTyped('') }}>
                  <input value={typed} onChange={event => setTyped(event.target.value)} placeholder="在这里输入文字，发送后自动回车（手机上更方便）" aria-label="发送到主机的文字" autoComplete="off" autoCapitalize="off" spellCheck={false} />
                  <button type="submit">发送</button>
                </form>
              : <span>仅查看。接管后可以用鼠标和键盘操作，交还之前助手会一直暂停。</span>}
            {error ? <span className="live-screen-error" role="alert">{error}</span> : <span>画面和按键都不会被记录。</span>}
          </footer>
        </section>
      </div>}
    </div>
  )
}
