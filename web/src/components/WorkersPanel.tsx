import { useCallback, useEffect, useRef, useState } from 'react'
import type { DragEvent as ReactDragEvent, PointerEvent as ReactPointerEvent } from 'react'
import '../ux/workers.css'

export type WorkerQuota = {
  state: 'available' | 'cooling'
  reset_at: number | null
  reason: string | null
}

export type UsageWindow = {
  name: string
  used_percent: number
  resets_at: number | null
}

export type WorkerUsage = {
  windows: UsageWindow[]
  plan: string | null
  fetched_at: number | null
  available: boolean
}

export type SwitchLogEntry = {
  at: number
  from: string | null
  to: string
  reason: 'threshold' | 'quota_error' | 'unavailable' | string
  detail?: string
}

export type Worker = {
  id: string
  label: string
  installed: boolean
  version: string | null
  path: string | null
  auth: 'ok' | 'needs_login' | 'unknown'
  login_command: string | null
  install_command: string | null
  install_supported: boolean
  runnable: boolean
  enabled: boolean
  priority: number
  quota: WorkerQuota
  models_supported?: boolean
  model?: string | null
  login_supported?: boolean
  auth_hint?: string | null
  provider?: { configured: boolean; explicit?: boolean; provider_name: string | null; model: string | null } | null
  usage?: WorkerUsage
  threshold_percent?: number
  over_threshold?: boolean
}

export type WorkersPayload = {
  enabled: boolean
  install_enabled: boolean
  workers: Worker[]
  active: string | null
  switch_log?: SwitchLogEntry[]
}

type Problem = { message: string; detail?: string }
type ServerError = { error?: string; code?: string; status?: number }
type LoginState = 'waiting_url' | 'open_url' | 'needs_code' | 'done' | 'failed'

const MODEL_SEARCH_MIN = 15
const QUOTA_TEXT = /RESOURCE_EXHAUSTED|quota|usage limit|rate limit|429|credit balance/i

function formatCoolingChip(resetAt: number | null): string {
  if (!resetAt) return '额度用完·冷却中'
  const leftSec = resetAt - Date.now() / 1000
  if (leftSec <= 0) return '额度用完·即将恢复'
  const minutes = Math.round(leftSec / 60)
  if (minutes < 60) return `额度用完·约 ${Math.max(1, minutes)} 分钟后恢复`
  const hours = Math.round(minutes / 60)
  return `额度用完·约 ${Math.max(1, hours)} 小时后恢复`
}

function formatRelativeTime(resetAt: number | null): string {
  if (!resetAt) return '冷却中'
  const leftSec = resetAt - Date.now() / 1000
  if (leftSec <= 0) return '即将恢复'
  const minutes = Math.round(leftSec / 60)
  if (minutes < 60) return `约 ${Math.max(1, minutes)} 分钟后恢复`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `约 ${Math.max(1, hours)} 小时后恢复`
  return `约 ${Math.round(hours / 24)} 天后恢复`
}

/** "3 小时后重置" style text for a usage window. */
function formatResetIn(resetsAt: number | null): string {
  if (!resetsAt) return ''
  const leftSec = resetsAt - Date.now() / 1000
  if (leftSec <= 0) return '即将重置'
  const minutes = Math.round(leftSec / 60)
  if (minutes < 60) return `${Math.max(1, minutes)} 分钟后重置`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours} 小时后重置`
  return `${Math.round(hours / 24)} 天后重置`
}

function formatAgo(at: number | null): string {
  if (!at) return ''
  const minutes = Math.round((Date.now() / 1000 - at) / 60)
  if (minutes < 1) return '刚刚更新'
  if (minutes < 60) return `${minutes} 分钟前更新`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours} 小时前更新`
  return `${Math.round(hours / 24)} 天前更新`
}

function formatLogTime(at: number): string {
  const d = new Date(at * 1000)
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  return `${d.getMonth() + 1}月${d.getDate()}日 ${hh}:${mm}`
}

function windowName(name: string): string {
  const key = name.toLowerCase()
  if (key === '5h') return '5 小时'
  if (key === 'weekly' || key === 'week') return '本周'
  if (key === 'daily' || key === 'day') return '今日'
  if (key === 'monthly' || key === 'month') return '本月'
  return name
}

/**
 * The switch-log detail is raw backend text (e.g. "Codex 用量 96% 已达阈值 95%" or "weekly 95%").
 * The sentence already starts with the worker's name, so keep only the window and the percentage.
 */
function describeUsageDetail(detail: string | undefined): string {
  const text = detail || ''
  const percent = text.match(/(\d{1,3})\s*%/)?.[1]
  const windowKey = text.match(/\b(5h|weekly|daily|monthly)\b/i)?.[1]
  const scope = windowKey ? `${windowName(windowKey)}用量` : '用量'
  return percent ? `${scope}已用 ${percent}%` : `${scope}已达上限`
}

function describeSwitch(entry: SwitchLogEntry, labelOf: (id: string | null) => string): string {
  const to = labelOf(entry.to)
  const from = entry.from ? labelOf(entry.from) : ''
  if (!from) return `开始使用 ${to}`
  if (entry.reason === 'threshold') return `${from} ${describeUsageDetail(entry.detail)}，换到 ${to}`
  if (entry.reason === 'quota_error') return `${from} 额度用完 → 换到 ${to}`
  if (entry.reason === 'unavailable') return `${from} 暂时不可用 → 换到 ${to}`
  return `${from} → 换到 ${to}`
}

/** When the over-threshold windows reset, i.e. when this worker is picked again. */
function overThresholdResetAt(worker: Worker): number | null {
  const windows = worker.usage?.windows || []
  const limit = worker.threshold_percent ?? 95
  const over = windows.filter(w => w.used_percent >= limit && w.resets_at)
  const pool = over.length ? over : windows.filter(w => w.resets_at)
  if (pool.length === 0) return null
  return Math.max(...pool.map(w => w.resets_at as number))
}

async function readError(response: Response): Promise<ServerError> {
  try {
    const body = await response.json()
    if (body && typeof body === 'object') {
      return {
        error: typeof body.error === 'string' ? body.error : undefined,
        code: typeof body.code === 'string' ? body.code : undefined,
        status: response.status,
      }
    }
  } catch {
    // ignore json parse error
  }
  return { error: `HTTP ${response.status}`, status: response.status }
}

function describeServerError(err: ServerError, fallback: string): Problem {
  if (err.code === 'install_disabled') return { message: '这台电脑没有开启在线安装功能。', detail: err.code }
  if (err.code === 'install_unsupported') return { message: '该执行者不支持在线安装。', detail: err.code }
  if (err.code === 'confirm_required') return { message: '安装操作需要确认。', detail: err.code }
  if (err.code === 'unknown_worker') return { message: '未找到指定的执行者。', detail: err.code }
  if (err.code === 'bad_code') return { message: '登录码格式不正确，请检查后重新提交。', detail: err.code }
  if (err.code === 'unknown_model') return { message: '未找到指定的模型。', detail: err.code }
  if (err.code === 'models_unsupported') return { message: '该执行者不支持切换模型。', detail: err.code }
  if (err.code === 'bad_threshold') return { message: '换班线需要是 1 到 100 之间的整数。', detail: err.code }
  if (err.error === 'not found') return { message: '执行者功能未开启。' }
  return { message: fallback, detail: err.code || err.error }
}

function humanCoolingReason(reason: string | null): { text: string; raw: string | null } {
  if (!reason) return { text: '', raw: null }
  if (QUOTA_TEXT.test(reason)) return { text: '这个账号的额度已经用完。', raw: reason }
  return { text: '这个执行者暂时不可用。', raw: reason }
}

/** Keep the owner's unsaved order and on/off switches when fresh data arrives. */
function mergePending(prev: Worker[], next: Worker[]): Worker[] {
  const byId = new Map(next.map(w => [w.id, w]))
  const out: Worker[] = []
  for (const p of prev) {
    const n = byId.get(p.id)
    if (n) {
      out.push({ ...n, enabled: p.enabled })
      byId.delete(p.id)
    }
  }
  for (const n of next) if (byId.has(n.id)) out.push(n)
  return out
}

function trimLabel(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text
}

type LoginSessionState = {
  session: string
  state: LoginState | 'cancelled'
  url: string | null
  message: string
  codeDraft: string
  codeError: string
  submittingCode: boolean
  canceling: boolean
}

type WorkerModelsData = {
  models: { id: string; label: string }[]
  cached_at?: number
}

function findScroller(from: HTMLElement | null): HTMLElement | null {
  let node = from?.parentElement || null
  while (node && node !== document.body) {
    const overflowY = getComputedStyle(node).overflowY
    if ((overflowY === 'auto' || overflowY === 'scroll') && node.scrollHeight > node.clientHeight + 1) return node
    node = node.parentElement
  }
  return null
}

function ThresholdControl({ worker, saving, onSave }: {
  worker: Worker
  saving: boolean
  onSave: (id: string, percent: number) => Promise<void>
}) {
  const saved = worker.threshold_percent ?? 95
  const [value, setValue] = useState(String(saved))
  const [error, setError] = useState('')
  useEffect(() => { setValue(String(saved)); setError('') }, [saved])
  const changed = value.trim() !== String(saved)
  return (
    <form
      className="worker-threshold"
      noValidate
      onSubmit={event => {
        event.preventDefault()
        const n = Number(value)
        if (!Number.isInteger(n) || n < 50 || n > 100) {
          setError('请填 50 到 100 之间的整数。')
          return
        }
        setError('')
        void onSave(worker.id, n)
      }}
    >
      <label className="worker-threshold-line">
        <span>到</span>
        <input
          type="number"
          inputMode="numeric"
          min={50}
          max={100}
          step={1}
          className="guard-input worker-threshold-input"
          aria-label={`${worker.label} 换班线百分比`}
          value={value}
          disabled={saving}
          onChange={e => { setValue(e.target.value); setError('') }}
        />
        <span>% 时换下一个</span>
      </label>
      {changed && (
        <button type="submit" className="guard-btn guard-btn-small guard-btn-primary" disabled={saving} aria-label={`保存 ${worker.label} 的换班线`}>
          {saving ? '保存中…' : '保存'}
        </button>
      )}
      {error && <span className="field-error worker-threshold-error" role="alert">{error}</span>}
    </form>
  )
}

function UsageBlock({ worker }: { worker: Worker }) {
  const usage = worker.usage
  if (!usage) return null
  const limit = worker.threshold_percent ?? 95
  if (!usage.available || usage.windows.length === 0) {
    return <p className="worker-usage-unavailable guard-sub">实时用量不可用，额度用完时自动切换</p>
  }
  return (
    <div className="worker-usage" role="group" aria-label={`${worker.label} 用量`}>
      {usage.windows.map(w => {
        const pct = Math.max(0, Math.min(100, Math.round(w.used_percent)))
        const tone = w.used_percent >= limit ? 'is-over' : w.used_percent >= limit - 15 ? 'is-warn' : 'is-ok'
        const reset = formatResetIn(w.resets_at)
        return (
          <div className="usage-row" key={w.name}>
            <span className="usage-label">{windowName(w.name)} <b>{pct}%</b></span>
            <div
              className={`usage-bar ${tone}`}
              role="progressbar"
              aria-label={`${windowName(w.name)}用量`}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={pct}
            >
              <span className="usage-fill" style={{ width: `${pct}%` }} />
              <i className="usage-limit" style={{ left: `${Math.min(100, limit)}%` }} aria-hidden="true" />
            </div>
            <span className="usage-reset">{reset}</span>
          </div>
        )
      })}
      <p className="worker-usage-meta guard-sub">
        {usage.plan ? `套餐：${usage.plan}` : ''}
        {usage.plan && usage.fetched_at ? ' · ' : ''}
        {formatAgo(usage.fetched_at)}
      </p>
    </div>
  )
}

export function WorkersPanel({ token }: { token: string }) {
  const [workers, setWorkers] = useState<Worker[]>([])
  const [activeWorkerId, setActiveWorkerId] = useState<string | null>(null)
  const [switchLog, setSwitchLog] = useState<SwitchLogEntry[]>([])
  const [installEnabled, setInstallEnabled] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const [featureOff, setFeatureOff] = useState(false)
  const [loadProblem, setLoadProblem] = useState<Problem | null>(null)
  const [actionProblem, setActionProblem] = useState<Problem | null>(null)
  const [notice, setNotice] = useState('')
  const [detecting, setDetecting] = useState(false)
  const [refreshingUsage, setRefreshingUsage] = useState(false)
  const [savingOrder, setSavingOrder] = useState(false)
  const [orderDirty, setOrderDirtyState] = useState(false)
  const [savingThresholdId, setSavingThresholdId] = useState('')
  const [confirmInstallId, setConfirmInstallId] = useState('')
  const [installingId, setInstallingId] = useState('')
  const [installOutput, setInstallOutput] = useState<Record<string, { ok: boolean; output: string }>>({})
  const [copiedCommand, setCopiedCommand] = useState('')
  const [resettingId, setResettingId] = useState('')
  const [loginSessions, setLoginSessions] = useState<Record<string, LoginSessionState>>({})
  const [startingLoginId, setStartingLoginId] = useState('')
  const [workerModels, setWorkerModels] = useState<Record<string, WorkerModelsData>>({})
  const [modelStatus, setModelStatus] = useState<Record<string, 'loading' | 'failed'>>({})
  const [modelFilter, setModelFilter] = useState<Record<string, string>>({})
  const [savingModelId, setSavingModelId] = useState('')
  const [savedModelTickId, setSavedModelTickId] = useState('')
  const [dragId, setDragId] = useState<string | null>(null)
  const [dropIndex, setDropIndex] = useState<number | null>(null)
  const [dragDy, setDragDy] = useState(0)

  const dirtyRef = useRef(false)
  const dragRef = useRef<string | null>(null)
  const lastYRef = useRef(0)
  const scrollerRef = useRef<HTMLElement | null>(null)
  const rafRef = useRef(0)
  const cardRefs = useRef(new Map<string, HTMLLIElement>())
  const workersRef = useRef<Worker[]>([])
  workersRef.current = workers

  const setOrderDirty = useCallback((value: boolean) => {
    dirtyRef.current = value
    setOrderDirtyState(value)
  }, [])

  const applyPayload = useCallback((data: WorkersPayload, replace = false) => {
    const list = Array.isArray(data.workers) ? data.workers : []
    setWorkers(prev => (dirtyRef.current && !replace ? mergePending(prev, list) : list))
    setActiveWorkerId(data.active || null)
    setInstallEnabled(Boolean(data.install_enabled))
    if (Array.isArray(data.switch_log)) setSwitchLog(data.switch_log)
  }, [])

  const call = useCallback(async (path: string, method = 'GET', body?: unknown) => {
    const headers: Record<string, string> = { Authorization: 'Bearer ' + token }
    if (body !== undefined) headers['Content-Type'] = 'application/json'
    const response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: method === 'GET' ? 'no-store' : undefined,
    })
    if (!response.ok) return { ok: false as const, status: response.status, data: null, err: await readError(response) }
    const data = await response.json().catch(() => null) as unknown
    return { ok: true as const, status: response.status, data, err: {} as ServerError }
  }, [token])

  const load = useCallback(async () => {
    try {
      const res = await call('/api/workers')
      if (res.status === 404) {
        setFeatureOff(true)
        setLoadProblem(null)
        return
      }
      if (!res.ok) {
        setLoadProblem(describeServerError(res.err, '无法读取执行者列表，请稍后重试。'))
        return
      }
      applyPayload(res.data as WorkersPayload)
      setFeatureOff(false)
      setLoadProblem(null)
    } catch {
      setLoadProblem({ message: '无法连接服务器，执行者列表暂时读取不到。' })
    } finally {
      setLoaded(true)
    }
  }, [call, applyPayload])

  useEffect(() => {
    setLoaded(false)
    void load()
  }, [load])

  const labelOf = useCallback((id: string | null) => {
    if (!id) return ''
    return workersRef.current.find(w => w.id === id)?.label || id
  }, [])

  useEffect(() => {
    const activeIds = Object.keys(loginSessions).filter(id => {
      const s = loginSessions[id]
      return s && (s.state === 'waiting_url' || s.state === 'open_url' || s.state === 'needs_code')
    })
    if (activeIds.length === 0) return

    const interval = setInterval(() => {
      activeIds.forEach(async id => {
        const current = loginSessions[id]
        if (!current || !current.session) return
        try {
          const res = await call(`/api/workers/${encodeURIComponent(id)}/login/${encodeURIComponent(current.session)}`)
          if (!res.ok) {
            const expired = res.err.code === 'session_not_found' || res.status === 404
            setLoginSessions(prev => {
              const cur = prev[id]
              if (!cur || cur.session !== current.session) return prev
              return {
                ...prev,
                [id]: {
                  ...cur,
                  state: 'failed',
                  message: expired ? '登录已超时，请重新开始登录。' : '检查登录状态失败，请重试。',
                },
              }
            })
            return
          }
          const data = res.data as { state: LoginState; url?: string | null; message?: string }
          setLoginSessions(prev => {
            const cur = prev[id]
            if (!cur || cur.session !== current.session || cur.state === 'cancelled') return prev
            return {
              ...prev,
              [id]: {
                ...cur,
                state: data.state,
                url: data.url !== undefined ? (data.url || null) : cur.url,
                message: data.message !== undefined ? (data.message || '') : cur.message,
              },
            }
          })
          if (data.state === 'done') void load()
        } catch {
          // keep polling
        }
      })
    }, 2000)

    return () => clearInterval(interval)
  }, [loginSessions, call, load])

  async function detect() {
    setDetecting(true)
    setActionProblem(null)
    setNotice('')
    const hadPending = dirtyRef.current
    try {
      const res = await call('/api/workers/detect', 'POST')
      if (!res.ok) {
        setActionProblem(describeServerError(res.err, '检测执行者失败，请稍后重试。'))
        return
      }
      applyPayload(res.data as WorkersPayload)
      setNotice(hadPending ? '已完成检测。你还没保存的顺序和开关改动都还在，点「保存顺序」才会生效。' : '已完成执行者检测。')
    } catch {
      setActionProblem({ message: '无法连接服务器，检测未完成。' })
    } finally {
      setDetecting(false)
    }
  }

  async function refreshUsage() {
    setRefreshingUsage(true)
    setActionProblem(null)
    setNotice('')
    try {
      const res = await call('/api/workers/usage/refresh', 'POST')
      if (!res.ok) {
        setActionProblem(describeServerError(res.err, '刷新用量失败，请稍后重试。'))
        return
      }
      applyPayload(res.data as WorkersPayload)
      setNotice('已刷新用量。')
    } catch {
      setActionProblem({ message: '无法连接服务器，用量没有刷新。' })
    } finally {
      setRefreshingUsage(false)
    }
  }

  function moveWorker(index: number, direction: -1 | 1) {
    const target = index + direction
    if (target < 0 || target >= workers.length) return
    const next = [...workers]
    const [moved] = next.splice(index, 1)
    next.splice(target, 0, moved)
    setWorkers(next)
    setOrderDirty(true)
  }

  /** Move worker `id` so it lands before the slot `insertAt` (0..n) of the current list. */
  function reorderTo(id: string, insertAt: number) {
    const list = workersRef.current
    const from = list.findIndex(w => w.id === id)
    if (from < 0) return
    const to = insertAt > from ? insertAt - 1 : insertAt
    if (to === from) return
    const next = [...list]
    const [moved] = next.splice(from, 1)
    next.splice(to, 0, moved)
    setWorkers(next)
    setOrderDirty(true)
  }

  function slotFromY(y: number): number {
    const list = workersRef.current
    for (let i = 0; i < list.length; i++) {
      const el = cardRefs.current.get(list[i].id)
      if (!el) continue
      const rect = el.getBoundingClientRect()
      const top = list[i].id === dragRef.current ? rect.top - dragDyRef.current : rect.top
      if (y < top + rect.height / 2) return i
    }
    return list.length
  }
  const dragDyRef = useRef(0)

  function autoScrollTick() {
    if (!dragRef.current) return
    const y = lastYRef.current
    const scroller = scrollerRef.current
    const top = scroller ? scroller.getBoundingClientRect().top : 0
    const bottom = scroller ? scroller.getBoundingClientRect().bottom : window.innerHeight
    const edge = 72
    let delta = 0
    if (y < top + edge) delta = -Math.ceil(((top + edge - y) / edge) * 18)
    else if (y > bottom - edge) delta = Math.ceil(((y - (bottom - edge)) / edge) * 18)
    if (delta) {
      if (scroller) scroller.scrollTop += delta
      else window.scrollBy(0, delta)
    }
    rafRef.current = requestAnimationFrame(autoScrollTick)
  }

  function beginDrag(id: string, startY: number) {
    dragRef.current = id
    lastYRef.current = startY
    scrollerRef.current = findScroller(cardRefs.current.get(id) || null)
    setDragId(id)
    cancelAnimationFrame(rafRef.current)
    rafRef.current = requestAnimationFrame(autoScrollTick)
  }

  function endDrag() {
    dragRef.current = null
    dragDyRef.current = 0
    cancelAnimationFrame(rafRef.current)
    setDragId(null)
    setDropIndex(null)
    setDragDy(0)
  }

  useEffect(() => () => cancelAnimationFrame(rafRef.current), [])

  // Mouse: native HTML5 drag and drop from the handle.
  function onHandleDragStart(event: ReactDragEvent, id: string) {
    event.dataTransfer.effectAllowed = 'move'
    event.dataTransfer.setData('text/plain', id)
    const card = cardRefs.current.get(id)
    if (card) event.dataTransfer.setDragImage(card, 24, 24)
    beginDrag(id, event.clientY)
  }

  function onListDragOver(event: ReactDragEvent) {
    if (!dragRef.current) return
    event.preventDefault()
    event.dataTransfer.dropEffect = 'move'
    lastYRef.current = event.clientY
    setDropIndex(slotFromY(event.clientY))
  }

  function onListDrop(event: ReactDragEvent) {
    const id = dragRef.current
    if (!id) return
    event.preventDefault()
    reorderTo(id, slotFromY(event.clientY))
    endDrag()
  }

  // Touch / pen: pointer events on the handle (touch-action: none keeps the page from scrolling).
  function onHandlePointerDown(event: ReactPointerEvent, id: string) {
    if (event.pointerType === 'mouse') return
    event.preventDefault()
    const startY = event.clientY
    beginDrag(id, startY)
    const move = (ev: PointerEvent) => {
      lastYRef.current = ev.clientY
      dragDyRef.current = ev.clientY - startY
      setDragDy(ev.clientY - startY)
      setDropIndex(slotFromY(ev.clientY))
    }
    const finish = (ev: PointerEvent) => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', finish)
      window.removeEventListener('pointercancel', finish)
      if (ev.type === 'pointerup' && dragRef.current === id) reorderTo(id, slotFromY(ev.clientY))
      endDrag()
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', finish)
    window.addEventListener('pointercancel', finish)
  }

  function toggleWorker(id: string) {
    setWorkers(prev => prev.map(w => w.id === id ? { ...w, enabled: !w.enabled } : w))
    setOrderDirty(true)
  }

  async function saveOrder() {
    // Nothing unsaved: no request (the button is disabled too).
    if (!dirtyRef.current) return
    setSavingOrder(true)
    setActionProblem(null)
    setNotice('')
    try {
      const res = await call('/api/workers/order', 'POST', {
        order: workers.map(w => w.id),
        enabled: workers.filter(w => w.enabled).map(w => w.id),
      })
      if (!res.ok) {
        setActionProblem(describeServerError(res.err, '保存执行者顺序失败，请稍后重试。'))
        return
      }
      setOrderDirty(false)
      applyPayload(res.data as WorkersPayload, true)
      setNotice('已保存执行者顺序。')
    } catch {
      setActionProblem({ message: '无法连接服务器，顺序没有保存。' })
    } finally {
      setSavingOrder(false)
    }
  }

  async function saveThreshold(id: string, percent: number) {
    setSavingThresholdId(id)
    setActionProblem(null)
    setNotice('')
    try {
      const res = await call(`/api/workers/${encodeURIComponent(id)}/threshold`, 'POST', { threshold_percent: percent })
      if (!res.ok) {
        setActionProblem(describeServerError(res.err, '保存换班线失败，请稍后重试。'))
        return
      }
      applyPayload(res.data as WorkersPayload)
      setNotice(`已保存：${labelOf(id)} 用量到 ${percent}% 时换下一个。`)
    } catch {
      setActionProblem({ message: '无法连接服务器，换班线没有保存。' })
    } finally {
      setSavingThresholdId('')
    }
  }

  async function installWorker(worker: Worker) {
    setInstallingId(worker.id)
    setActionProblem(null)
    try {
      const response = await fetch(`/api/workers/${encodeURIComponent(worker.id)}/install`, {
        method: 'POST',
        headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirm: true }),
      })
      const data = await response.json().catch(() => null) as { ok?: boolean; output?: string; error?: string; code?: string } | null
      const output = data?.output || data?.error || (response.ok ? '安装完成。' : `HTTP ${response.status}`)
      setInstallOutput(prev => ({
        ...prev,
        [worker.id]: { ok: Boolean(data?.ok), output },
      }))
      if (!response.ok || !data?.ok) {
        setActionProblem(describeServerError(data || { error: `HTTP ${response.status}` }, `安装 ${worker.label} 失败。`))
      } else {
        setNotice(`已安装 ${worker.label}。`)
        await load()
      }
    } catch {
      setActionProblem({ message: `无法连接服务器，${worker.label} 安装失败。` })
    } finally {
      setInstallingId('')
      setConfirmInstallId('')
    }
  }

  async function resetQuota(id: string) {
    setResettingId(id)
    setActionProblem(null)
    try {
      const res = await call(`/api/workers/${encodeURIComponent(id)}/reset-quota`, 'POST')
      if (!res.ok) {
        setActionProblem(describeServerError(res.err, '清除额度冷却失败。'))
        return
      }
      applyPayload(res.data as WorkersPayload)
      setNotice(`已重置 ${labelOf(id) || id} 的额度状态。`)
    } catch {
      setActionProblem({ message: '无法连接服务器，清除额度冷却失败。' })
    } finally {
      setResettingId('')
    }
  }

  async function startLogin(workerId: string) {
    setStartingLoginId(workerId)
    setActionProblem(null)
    try {
      const res = await call(`/api/workers/${encodeURIComponent(workerId)}/login/start`, 'POST')
      if (!res.ok) {
        setActionProblem(describeServerError(res.err, '发起登录失败，请稍后重试。'))
        return
      }
      const data = res.data as { session: string; state: LoginState; url?: string | null; message?: string }
      setLoginSessions(prev => ({
        ...prev,
        [workerId]: {
          session: data.session,
          state: data.state || 'waiting_url',
          url: data.url || null,
          message: data.message || '',
          codeDraft: '',
          codeError: '',
          submittingCode: false,
          canceling: false,
        },
      }))
      if (data.state === 'done') await load()
    } catch {
      setActionProblem({ message: '无法连接服务器，发起登录失败。' })
    } finally {
      setStartingLoginId('')
    }
  }

  async function submitCode(workerId: string) {
    const session = loginSessions[workerId]
    if (!session || !session.codeDraft.trim() || session.submittingCode) return
    setLoginSessions(prev => ({
      ...prev,
      [workerId]: { ...prev[workerId], submittingCode: true, codeError: '' },
    }))
    try {
      const code = session.codeDraft.trim()
      const res = await call(
        `/api/workers/${encodeURIComponent(workerId)}/login/${encodeURIComponent(session.session)}/code`,
        'POST',
        { code },
      )
      if (!res.ok) {
        // A wrong-looking code must not kill the login: keep the draft and ask again.
        const expired = res.err.code === 'session_not_found' || res.status === 404
        setLoginSessions(prev => ({
          ...prev,
          [workerId]: expired
            ? { ...prev[workerId], submittingCode: false, state: 'failed', message: '登录已超时，请重新开始登录。' }
            : {
              ...prev[workerId],
              submittingCode: false,
              codeError: res.err.code === 'bad_code' ? '登录码格式不正确，请检查后重新提交。' : '提交登录码失败，请重试。',
            },
        }))
        return
      }
      const check = await call(`/api/workers/${encodeURIComponent(workerId)}/login/${encodeURIComponent(session.session)}`)
      if (check.ok) {
        const data = check.data as { state: LoginState; url?: string | null; message?: string }
        setLoginSessions(prev => ({
          ...prev,
          [workerId]: {
            ...prev[workerId],
            submittingCode: false,
            state: data.state,
            url: data.url !== undefined ? (data.url || null) : prev[workerId].url,
            message: data.message !== undefined ? (data.message || '') : prev[workerId].message,
          },
        }))
        if (data.state === 'done') await load()
      } else {
        setLoginSessions(prev => ({
          ...prev,
          [workerId]: { ...prev[workerId], submittingCode: false },
        }))
      }
    } catch {
      setLoginSessions(prev => ({
        ...prev,
        [workerId]: { ...prev[workerId], submittingCode: false, codeError: '提交失败，请重试。' },
      }))
    }
  }

  async function cancelLogin(workerId: string) {
    const session = loginSessions[workerId]
    if (!session) return
    setLoginSessions(prev => ({
      ...prev,
      [workerId]: { ...prev[workerId], canceling: true },
    }))
    try {
      await call(`/api/workers/${encodeURIComponent(workerId)}/login/${encodeURIComponent(session.session)}/cancel`, 'POST')
    } catch {
      // the session expires on its own
    }
    setLoginSessions(prev => ({
      ...prev,
      [workerId]: { ...prev[workerId], canceling: false, state: 'cancelled', message: '已取消。' },
    }))
  }

  async function loadModels(workerId: string, force = false) {
    if ((workerModels[workerId] && !force) || modelStatus[workerId] === 'loading') return
    setModelStatus(prev => ({ ...prev, [workerId]: 'loading' }))
    try {
      const res = await call(`/api/workers/${encodeURIComponent(workerId)}/models`)
      if (res.ok && res.data && Array.isArray((res.data as WorkerModelsData).models)) {
        setWorkerModels(prev => ({ ...prev, [workerId]: res.data as WorkerModelsData }))
        setModelStatus(prev => { const next = { ...prev }; delete next[workerId]; return next })
      } else {
        setModelStatus(prev => ({ ...prev, [workerId]: 'failed' }))
      }
    } catch {
      setModelStatus(prev => ({ ...prev, [workerId]: 'failed' }))
    }
  }

  async function setWorkerModel(workerId: string, model: string | null) {
    setSavingModelId(workerId)
    setActionProblem(null)
    try {
      const res = await call(`/api/workers/${encodeURIComponent(workerId)}/model`, 'POST', { model })
      if (!res.ok) {
        setActionProblem(describeServerError(res.err, '保存模型设置失败。'))
        return
      }
      const data = res.data as WorkersPayload | null
      if (data && Array.isArray(data.workers)) applyPayload(data)
      else setWorkers(prev => prev.map(w => w.id === workerId ? { ...w, model } : w))
      setSavedModelTickId(workerId)
      setTimeout(() => {
        setSavedModelTickId(cur => cur === workerId ? '' : cur)
      }, 2000)
    } catch {
      setActionProblem({ message: '无法连接服务器，保存模型失败。' })
    } finally {
      setSavingModelId('')
    }
  }

  async function copyCommand(text: string) {
    let success = false
    try {
      if (navigator?.clipboard?.writeText) {
        await navigator.clipboard.writeText(text)
        success = true
      }
    } catch {
      // fallback
    }
    if (!success) {
      try {
        const ta = document.createElement('textarea')
        ta.value = text
        ta.style.position = 'fixed'
        ta.style.opacity = '0'
        document.body.appendChild(ta)
        ta.select()
        success = document.execCommand('copy')
        document.body.removeChild(ta)
      } catch {
        success = false
      }
    }
    setCopiedCommand(text)
    setTimeout(() => setCopiedCommand(cur => cur === text ? '' : cur), 2000)
  }

  /** One state chip per worker: what is wrong with it (or "可用"). */
  function renderStatusChip(worker: Worker) {
    if (!worker.installed) {
      return <span className="guard-badge guard-badge-muted">未安装</span>
    }
    if (worker.quota?.state === 'cooling') {
      return <span className="guard-badge guard-badge-danger">{formatCoolingChip(worker.quota.reset_at)}</span>
    }
    if (worker.over_threshold) {
      const at = overThresholdResetAt(worker)
      return <span className="guard-badge guard-sev-warning">已达上限{at ? ` · ${formatRelativeTime(at)}` : ''}</span>
    }
    if (worker.auth === 'needs_login') {
      return <span className="guard-badge guard-sev-warning">{worker.id === 'pi' ? '未配置服务商' : '需要登录'}</span>
    }
    if (worker.auth === 'unknown') {
      return <span className="guard-badge guard-badge-muted">登录状态未知</span>
    }
    return <span className="guard-badge guard-badge-ok">可用</span>
  }

  /** One role chip per worker: how rotation treats it. Hidden when the state chip already says "can't use". */
  function renderRoleChip(worker: Worker) {
    if (!worker.installed || worker.auth === 'needs_login') return null
    if (activeWorkerId === worker.id) return <span className="worker-role is-active">当前使用</span>
    if (!worker.runnable) return <span className="worker-role is-off">暂不支持轮换</span>
    if (!worker.enabled) return <span className="worker-role is-off">已停用</span>
    // Over the threshold or out of quota right now: rotation skips it until it recovers.
    if (worker.over_threshold || worker.quota?.state === 'cooling') return <span className="worker-role is-off">暂时跳过</span>
    return <span className="worker-role">参与轮换</span>
  }

  function renderGuide() {
    if (workers.length === 0) return null
    const anyInstalled = workers.some(w => w.installed)
    const usable = workers.filter(w => w.installed && w.runnable && w.enabled && w.auth !== 'needs_login')
    if (!anyInstalled) {
      return '第一步：先装好至少一个执行者。执行者就是替你干活的 AI 工具（比如 Codex、Claude Code），装好并登录后 Conveyor 才能用它。'
    }
    if (usable.length === 0) {
      return '第一步：现在还没有能用的执行者。请先登录标着「需要登录」的执行者，或者打开「已停用」的开关。'
    }
    return '任务会交给排在最前面、能用的执行者；它的用量到了换班线，就自动换下一个。拖动 ⠿ 可以调整顺序，调完点「保存顺序」。'
  }

  /** The built-in Pi has no login of its own: it uses the provider saved in 模型设置. */
  function openModelSettings() {
    const desktop = document.querySelector<HTMLElement>('.settings-button')
    if (desktop && desktop.offsetParent !== null) { desktop.click(); return }
    const more = Array.from(document.querySelectorAll<HTMLElement>('button.mobile-nav-item'))
      .find(button => button.getAttribute('aria-haspopup') === 'dialog')
    more?.click()
    window.setTimeout(() => {
      Array.from(document.querySelectorAll<HTMLElement>('.mobile-sheet-item'))
        .find(item => (item.textContent || '').includes('设置'))?.click()
    }, 80)
  }

  function renderPiProviderBox(worker: Worker) {
    if (worker.id !== 'pi') return null
    const provider = worker.provider
    const configured = Boolean(provider?.configured)
    const detail = provider?.provider_name
      ? `${provider.provider_name}${provider.model ? ` / ${provider.model}` : ''}`
      : ''
    return (
      <div className="worker-login-box" data-testid="pi-provider-box">
        {configured ? (
          <p className="guard-sub">
            内置 · 使用「模型设置」里的服务商：{detail || '已通过服务器环境变量配置'}
          </p>
        ) : (
          <>
            <p className="guard-sub">内置 · 使用「模型设置」里的服务商：未配置。{worker.auth_hint || '先在「设置 → 模型设置」里填服务商'}</p>
            <button type="button" className="guard-btn guard-btn-small" onClick={openModelSettings}>去模型设置</button>
          </>
        )}
      </div>
    )
  }

  function renderModelBox(worker: Worker) {
    if (worker.id === 'pi') return null
    if (!worker.installed || !worker.models_supported) return null
    if (!worker.runnable) {
      return (
        <div className="worker-model-box">
          <p className="guard-sub">这个执行者暂时不参与轮换，所以现在不能选模型。</p>
        </div>
      )
    }
    const data = workerModels[worker.id]
    const status = modelStatus[worker.id]
    const all = (data?.models || []).filter((m, i, arr) => arr.findIndex(x => x.id === m.id) === i)
    const filter = (modelFilter[worker.id] || '').trim().toLowerCase()
    const shown = filter
      ? all.filter(m => m.id.toLowerCase().includes(filter) || (m.label || '').toLowerCase().includes(filter))
      : all
    const searchable = all.length > MODEL_SEARCH_MIN
    const current = worker.model || ''
    return (
      <div className="worker-model-box">
        <div className="worker-model-row">
          <label htmlFor={`worker-model-${worker.id}`} className="guard-sub worker-model-label">
            模型：
          </label>
          {searchable && (
            <input
              type="search"
              className="guard-input worker-model-search"
              aria-label={`搜索 ${worker.label} 的模型`}
              placeholder={`搜索模型（共 ${all.length} 个）`}
              value={modelFilter[worker.id] || ''}
              onChange={e => setModelFilter(prev => ({ ...prev, [worker.id]: e.target.value }))}
            />
          )}
          <select
            id={`worker-model-${worker.id}`}
            aria-label={`${worker.label} 模型选择`}
            className="guard-select worker-model-select"
            value={current}
            onFocus={() => { void loadModels(worker.id) }}
            onClick={() => { void loadModels(worker.id) }}
            onChange={e => {
              const val = e.target.value === '' ? null : e.target.value
              void setWorkerModel(worker.id, val)
            }}
            disabled={savingModelId === worker.id}
          >
            <option value="">使用默认</option>
            {shown.map(m => (
              <option key={m.id} value={m.id}>
                {m.label && m.label !== m.id ? `${trimLabel(m.label, 70)} · ${m.id}` : m.id}
              </option>
            ))}
            {worker.model && !shown.some(m => m.id === worker.model) && (
              <option value={worker.model}>{worker.model}</option>
            )}
          </select>
          {savedModelTickId === worker.id && <span className="worker-saved-tick" role="status">✓ 已保存</span>}
        </div>
        {status === 'loading' && <p className="guard-sub worker-model-state" role="status">正在读取模型列表…</p>}
        {status === 'failed' && (
          <p className="worker-model-state worker-model-failed" role="alert">
            <span>读取模型列表失败。</span>
            <button type="button" className="guard-btn guard-btn-small" onClick={() => { void loadModels(worker.id, true) }}>重试</button>
          </p>
        )}
        {!status && data && (
          <p className="guard-sub worker-model-state">
            {filter ? `找到 ${shown.length} 个，共 ${all.length} 个模型` : `共 ${all.length} 个模型`}
          </p>
        )}
        {!status && !data && <p className="guard-sub worker-model-state">点一下选择框，读取可选的模型。</p>}
      </div>
    )
  }

  function renderTechDetail(problem: Problem) {
    if (!problem.detail) return null
    return (
      <details className="guard-tech-details">
        <summary>技术详情</summary>
        <span className="guard-tech">{problem.detail}</span>
      </details>
    )
  }

  if (featureOff) {
    return (
      <div className="guard-page guard-workers">
        <div className="guard-stack">
          <p className="guard-empty guard-muted">执行者功能未开启</p>
        </div>
      </div>
    )
  }

  const activeWorker = workers.find(w => w.id === activeWorkerId) || null
  const guide = loaded && !loadProblem ? renderGuide() : null
  const logEntries = [...switchLog].sort((a, b) => b.at - a.at)
  const busy = detecting || savingOrder || refreshingUsage
  const dragFrom = dragId ? workers.findIndex(w => w.id === dragId) : -1
  const showDrop = dragId !== null && dropIndex !== null && dropIndex !== dragFrom && dropIndex !== dragFrom + 1

  return (
    <div className="guard-page guard-workers">
      <div className="guard-stack workers-stack">
        {loaded && !loadProblem && workers.length > 0 && (
          <section className="workers-summary" aria-label="当前使用情况">
            <p className="workers-active" role="status">
              <span>当前使用：{activeWorker ? activeWorker.label : '暂时没有可用的执行者'}</span>
            </p>
            {guide && <p className={`workers-guide ${guide.startsWith('第一步') ? 'is-first-step' : ''}`}>{guide}</p>}
            <details className="workers-switch-log">
              <summary>切换记录{logEntries.length ? `（${logEntries.length}）` : ''}</summary>
              {logEntries.length === 0 ? (
                <p className="guard-sub">还没有换过执行者。</p>
              ) : (
                <ul>
                  {logEntries.map((entry, i) => (
                    <li key={`${entry.at}-${i}`}>
                      <span className="switch-time">{formatLogTime(entry.at)}</span>{' '}
                      <span>{describeSwitch(entry, labelOf)}</span>
                    </li>
                  ))}
                </ul>
              )}
            </details>
          </section>
        )}

        <div className="workers-actions-row" role="toolbar" aria-label="执行者操作">
          {orderDirty && (
            <p className="workers-dirty" role="status">
              <span className="guard-badge guard-sev-warning">未保存</span>
              <span>顺序或开关改过了，点「保存顺序」后才会生效</span>
            </p>
          )}
          <button
            type="button"
            className="guard-btn"
            disabled={busy}
            aria-busy={refreshingUsage || undefined}
            onClick={() => { void refreshUsage() }}
          >
            {refreshingUsage ? '刷新中…' : '刷新用量'}
          </button>
          <button
            type="button"
            className="guard-btn"
            disabled={busy}
            aria-busy={detecting || undefined}
            onClick={() => { void detect() }}
          >
            {detecting ? '检测中…' : '重新检测'}
          </button>
          <button
            type="button"
            className={`guard-btn ${orderDirty ? 'guard-btn-primary is-highlight' : ''}`}
            disabled={busy || !orderDirty}
            aria-busy={savingOrder || undefined}
            onClick={() => { void saveOrder() }}
          >
            {savingOrder ? '保存中…' : '保存顺序'}
          </button>
        </div>

        {(notice || actionProblem) && (
          <div aria-live="polite">
            {notice && <p role="status" className="guard-feedback guard-feedback-ok">{notice}</p>}
            {actionProblem && (
              <div role="alert" className="guard-feedback guard-feedback-error">
                {actionProblem.message}
                {renderTechDetail(actionProblem)}
              </div>
            )}
          </div>
        )}

        {!loaded ? (
          <div className="workers-loading" aria-busy="true">
            <p className="guard-sub" role="status">正在检测已安装的执行者，第一次可能要几秒钟…</p>
            <ul className="workers-list" aria-hidden="true">
              {[0, 1, 2].map(i => (
                <li key={i} className="worker-card is-skeleton">
                  <span className="sk-line sk-title" />
                  <span className="sk-line sk-wide" />
                  <span className="sk-line sk-mid" />
                </li>
              ))}
            </ul>
          </div>
        ) : loadProblem ? (
          <div className="guard-empty" role="alert">
            <p>{loadProblem.message}</p>
            {renderTechDetail(loadProblem)}
            <button type="button" className="guard-btn" onClick={() => { setLoaded(false); void load() }}>重试</button>
          </div>
        ) : workers.length === 0 ? (
          <p className="guard-empty">暂无可用执行者</p>
        ) : (
          <ul
            className={`workers-list ${dragId ? 'is-dragging' : ''}`}
            aria-label="执行者列表"
            onDragOver={onListDragOver}
            onDrop={onListDrop}
          >
            {workers.map((worker, index) => {
              const pv = [worker.version, worker.path].filter(Boolean).join(' · ')
              const isConfirmingInstall = confirmInstallId === worker.id
              const isInstalling = installingId === worker.id
              const isResetting = resettingId === worker.id
              const out = installOutput[worker.id]
              const session = loginSessions[worker.id]
              const cooling = worker.quota?.state === 'cooling'
              const coolingReason = humanCoolingReason(worker.quota?.reason ?? null)
              const showUsage = worker.installed && Boolean(worker.usage)
              const showThreshold = worker.installed && typeof worker.threshold_percent === 'number'
              const hasBody =
                !worker.installed ||
                Boolean(worker.installed && worker.models_supported) ||
                (Boolean(worker.login_supported) && (worker.auth === 'needs_login' || worker.auth === 'unknown')) ||
                Boolean(session) ||
                worker.auth === 'needs_login' ||
                worker.id === 'pi' ||
                cooling ||
                showUsage ||
                showThreshold ||
                Boolean(out)
              const dropClass =
                showDrop && dropIndex === index ? 'drop-before'
                  : showDrop && dropIndex === workers.length && index === workers.length - 1 ? 'drop-after' : ''

              return (
                <li
                  key={worker.id}
                  ref={el => { if (el) cardRefs.current.set(worker.id, el); else cardRefs.current.delete(worker.id) }}
                  className={`worker-card ${activeWorkerId === worker.id ? 'is-active' : ''} ${dragId === worker.id ? 'is-dragged' : ''} ${dropClass}`}
                  style={dragId === worker.id && dragDy ? { transform: `translateY(${dragDy}px)` } : undefined}
                  aria-label={worker.label}
                >
                  <div className="worker-card-head">
                    <div className="worker-meta">
                      <span
                        className="worker-drag-handle"
                        draggable
                        role="img"
                        aria-label={`拖动调整 ${worker.label} 的顺序`}
                        title="按住拖动，调整优先级"
                        onDragStart={event => onHandleDragStart(event, worker.id)}
                        onDragEnd={endDrag}
                        onPointerDown={event => onHandlePointerDown(event, worker.id)}
                      >
                        ⠿
                      </span>
                      <span className="worker-priority" title={`优先级第 ${index + 1}`}>#{index + 1}</span>
                      <strong className="worker-label">{worker.label}</strong>
                      {renderStatusChip(worker)}
                      {renderRoleChip(worker)}
                    </div>
                    <div className="worker-controls">
                      <label className="worker-toggle" title={worker.enabled ? '已启用，参与轮换' : '已停用，不参与轮换'}>
                        <input
                          type="checkbox"
                          checked={worker.enabled}
                          onChange={() => toggleWorker(worker.id)}
                          aria-label={`启用 ${worker.label}`}
                        />
                        <span className="worker-toggle-slider" aria-hidden="true" />
                        <span className="worker-toggle-text">{worker.enabled ? '已启用' : '已停用'}</span>
                      </label>
                      <div className="worker-order-btns" role="group" aria-label={`${worker.label} 顺序调整`}>
                        <button
                          type="button"
                          className="guard-btn guard-btn-small worker-order-btn"
                          disabled={index === 0}
                          aria-label={`上移 ${worker.label}`}
                          onClick={() => moveWorker(index, -1)}
                        >
                          <span aria-hidden="true">↑</span> 上移
                        </button>
                        <button
                          type="button"
                          className="guard-btn guard-btn-small worker-order-btn"
                          disabled={index === workers.length - 1}
                          aria-label={`下移 ${worker.label}`}
                          onClick={() => moveWorker(index, 1)}
                        >
                          <span aria-hidden="true">↓</span> 下移
                        </button>
                      </div>
                    </div>
                  </div>

                  {pv && (
                    <details className="guard-tech-details worker-tech">
                      <summary>技术详情</summary>
                      <span className="guard-tech worker-path-version">{pv}</span>
                    </details>
                  )}

                  {hasBody && (
                    <div className="worker-body">
                      {(showUsage || showThreshold) && (
                        <div className="worker-usage-section">
                          <UsageBlock worker={worker} />
                          {showThreshold && (
                            <ThresholdControl
                              worker={worker}
                              saving={savingThresholdId === worker.id}
                              onSave={saveThreshold}
                            />
                          )}
                        </div>
                      )}

                      {!worker.installed && (
                        <>
                          {worker.install_supported && installEnabled && worker.install_command ? (
                            <div className="worker-install-section">
                              {!isConfirmingInstall ? (
                                <div className="worker-install-action">
                                  <button
                                    type="button"
                                    className="guard-btn guard-btn-small"
                                    onClick={() => { setConfirmInstallId(worker.id); setActionProblem(null) }}
                                  >
                                    安装
                                  </button>
                                </div>
                              ) : (
                                <div className="guard-inline-confirm" role="group" aria-label={`确认安装 ${worker.label}`}>
                                  <div className="worker-install-confirm-text">
                                    <p>将从官方渠道下载并安装 {worker.label}，大约需要几分钟，期间请保持页面打开。</p>
                                    <details className="worker-command-details">
                                      <summary>查看将运行的命令</summary>
                                      <code className="worker-code">{worker.install_command}</code>
                                    </details>
                                  </div>
                                  <div className="guard-inline-confirm-actions">
                                    <button
                                      type="button"
                                      className="guard-btn guard-btn-small"
                                      disabled={isInstalling}
                                      onClick={() => setConfirmInstallId('')}
                                    >
                                      取消
                                    </button>
                                    <button
                                      type="button"
                                      className="guard-btn guard-btn-small guard-btn-primary"
                                      disabled={isInstalling}
                                      onClick={() => { void installWorker(worker) }}
                                    >
                                      {isInstalling ? '安装中…' : '确认安装'}
                                    </button>
                                  </div>
                                </div>
                              )}
                            </div>
                          ) : worker.install_command ? (
                            <div className="worker-command-box">
                              <p className="guard-sub">在运行 Conveyor 的电脑上打开终端，运行下面这条命令安装：</p>
                              <div className="worker-command-row">
                                <code className="worker-code">{worker.install_command}</code>
                                <button
                                  type="button"
                                  className="guard-btn guard-btn-small"
                                  onClick={() => { void copyCommand(worker.install_command!) }}
                                >
                                  {copiedCommand === worker.install_command ? '已复制' : '复制'}
                                </button>
                              </div>
                            </div>
                          ) : (
                            <div className="worker-manual-hint">
                              <p className="guard-sub">{worker.id === 'agy' ? '请从 Antigravity 官方渠道安装' : '需要手动安装，暂时没有一键安装'}</p>
                            </div>
                          )}
                        </>
                      )}

                      {out && (
                        <details className="worker-details" open>
                          <summary>详情</summary>
                          <pre className="worker-pre">{out.output}</pre>
                        </details>
                      )}

                      {worker.installed && (worker.login_supported || session) && (worker.auth === 'needs_login' || worker.auth === 'unknown' || session) && (
                        <div className="worker-login-box">
                          {!session ? (
                            <div className="worker-login-action">
                              <button
                                type="button"
                                className="guard-btn guard-btn-small guard-btn-primary"
                                disabled={startingLoginId === worker.id}
                                onClick={() => { void startLogin(worker.id) }}
                              >
                                {startingLoginId === worker.id ? '正在准备登录…' : '登录'}
                              </button>
                            </div>
                          ) : (
                            <div className="worker-login-flow" role="group" aria-label={`${worker.label} 登录流程`}>
                              {(session.state === 'waiting_url' || (startingLoginId === worker.id && !session.url)) && (
                                <div className="worker-login-waiting">
                                  <span className="guard-muted">正在准备登录…</span>
                                  <button
                                    type="button"
                                    className="guard-btn guard-btn-small"
                                    disabled={session.canceling}
                                    onClick={() => { void cancelLogin(worker.id) }}
                                  >
                                    取消
                                  </button>
                                </div>
                              )}

                              {(session.state === 'open_url' || (session.state === 'needs_code' && session.url)) && (
                                <div className="worker-login-open-section">
                                  <div className="worker-login-url-row">
                                    {session.url && (
                                      <a
                                        href={session.url}
                                        target="_blank"
                                        rel="noopener"
                                        role="button"
                                        className="guard-btn guard-btn-small guard-btn-primary"
                                      >
                                        打开登录页面
                                      </a>
                                    )}
                                    <button
                                      type="button"
                                      className="guard-btn guard-btn-small"
                                      disabled={session.canceling}
                                      onClick={() => { void cancelLogin(worker.id) }}
                                    >
                                      取消
                                    </button>
                                  </div>
                                  <p className="guard-sub worker-login-hint">在浏览器打开的页面完成授权后返回此处</p>
                                </div>
                              )}

                              {session.state === 'needs_code' && (
                                <form
                                  className="worker-login-code-form"
                                  onSubmit={e => {
                                    e.preventDefault()
                                    void submitCode(worker.id)
                                  }}
                                >
                                  <input
                                    type="text"
                                    className="guard-input worker-login-code-input"
                                    placeholder="把登录页面显示的登录码粘贴到这里"
                                    aria-label="把登录页面显示的登录码粘贴到这里"
                                    aria-invalid={Boolean(session.codeError)}
                                    value={session.codeDraft}
                                    disabled={session.submittingCode}
                                    onChange={e => {
                                      const val = e.target.value
                                      setLoginSessions(prev => ({
                                        ...prev,
                                        [worker.id]: { ...prev[worker.id], codeDraft: val, codeError: '' },
                                      }))
                                    }}
                                    autoFocus
                                  />
                                  <div className="worker-login-code-actions">
                                    <button
                                      type="submit"
                                      className="guard-btn guard-btn-small guard-btn-primary"
                                      disabled={!session.codeDraft.trim() || session.submittingCode}
                                    >
                                      {session.submittingCode ? '提交中…' : '提交'}
                                    </button>
                                    {!session.url && (
                                      <button
                                        type="button"
                                        className="guard-btn guard-btn-small"
                                        disabled={session.canceling}
                                        onClick={() => { void cancelLogin(worker.id) }}
                                      >
                                        取消
                                      </button>
                                    )}
                                  </div>
                                  {session.codeError && <p className="field-error worker-code-error" role="alert">{session.codeError}</p>}
                                </form>
                              )}

                              {session.state === 'done' && (
                                <p className="guard-feedback guard-feedback-ok" role="status">
                                  {session.message || '登录成功。'}
                                </p>
                              )}

                              {session.state === 'cancelled' && (
                                <div className="worker-login-failed-row">
                                  <span className="guard-sub worker-login-cancelled">已取消。</span>
                                  <button
                                    type="button"
                                    className="guard-btn guard-btn-small"
                                    onClick={() => { void startLogin(worker.id) }}
                                  >
                                    重新开始登录
                                  </button>
                                </div>
                              )}

                              {session.state === 'failed' && (
                                <div className="worker-login-failed-row" role="alert">
                                  <span className="guard-feedback guard-feedback-error">
                                    {session.message || '登录失败，请重试。'}
                                  </span>
                                  <button
                                    type="button"
                                    className="guard-btn guard-btn-small"
                                    onClick={() => { void startLogin(worker.id) }}
                                  >
                                    重试
                                  </button>
                                </div>
                              )}
                            </div>
                          )}
                        </div>
                      )}

                      {worker.installed && worker.id !== 'pi' && !worker.login_supported && !session && worker.auth === 'needs_login' && (
                        <div className="worker-login-box">
                          <p className="guard-sub">在运行 Conveyor 的电脑上打开终端，运行下面这条命令并按提示在浏览器登录，完成后点「重新检测」：</p>
                          {worker.login_command && (
                            <div className="worker-command-row">
                              <code className="worker-code">{worker.login_command}</code>
                              <button
                                type="button"
                                className="guard-btn guard-btn-small"
                                onClick={() => { void copyCommand(worker.login_command!) }}
                              >
                                {copiedCommand === worker.login_command ? '已复制' : '复制'}
                              </button>
                            </div>
                          )}
                        </div>
                      )}

                      {worker.installed && renderPiProviderBox(worker)}

                      {renderModelBox(worker)}

                      {worker.installed && cooling && (
                        <div className="worker-cooling-box">
                          <div className="worker-cooling-row">
                            <div className="worker-cooling-info">
                              <span className="guard-sub">预计恢复时间：{formatRelativeTime(worker.quota.reset_at)}</span>
                              {coolingReason.text && <span className="guard-sub worker-cooling-reason">{coolingReason.text}</span>}
                              {coolingReason.raw && (
                                <details className="guard-tech-details">
                                  <summary>技术详情</summary>
                                  <span className="guard-tech">{coolingReason.raw}</span>
                                </details>
                              )}
                            </div>
                            <div className="worker-cooling-action">
                              <button
                                type="button"
                                className="guard-btn guard-btn-small guard-btn-quiet worker-reset-btn"
                                disabled={isResetting}
                                onClick={() => { void resetQuota(worker.id) }}
                              >
                                {isResetting ? '处理中…' : '我已处理，立即重试'}
                              </button>
                              <span className="guard-sub">只会清掉这里的冷却标记；如果额度还没恢复，下一个任务还会失败。</span>
                            </div>
                          </div>
                        </div>
                      )}
                    </div>
                  )}
                </li>
              )
            })}
          </ul>
        )}
      </div>
    </div>
  )
}
