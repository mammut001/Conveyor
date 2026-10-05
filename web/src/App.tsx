import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ChatPanel } from './components/ChatPanel'
import { FormattedText } from './components/FormattedText'
import { InboxPanel } from './components/InboxPanel'
import { MemoryPanel } from './components/MemoryPanel'
import { SkillsPanel } from './components/SkillsPanel'
import { ConnectorsPanel } from './components/ConnectorsPanel'
import { ApprovalInboxPanel } from './components/ApprovalInboxPanel'
import { TeammatePanel } from './components/TeammatePanel'
import { RuntimeOwnerCard } from './components/RuntimeOwnerCard'
import { TranscriptPanel } from './components/TranscriptPanel'
import { runtimeOwnerFromJob, terminalJobState, type TranscriptMessage } from './runtime'
import { dropApproval, isStale, shouldRefreshForEvent } from './approvalFreshness'

type EventItem = {
  schema_version: number; event_id: string; sequence: number; timestamp: string
  kind: string; job_id: string; payload: Record<string, unknown>; tool_call_id?: string
}
type Job = {
  id: string; state: string; mode: string; channel: string; chat_id: string; operator_id?: string
  created_at: string; updated_at?: string; started_at?: string; finished_at?: string
  prompt_preview: string; metadata?: Record<string, unknown>; latest_event?: EventItem
  error?: string
  changed_files?: { status: string; path: string }[]
  runtime?: Record<string, unknown>
}
type Session = {
  id: string; channel?: string; title?: string; created_at: string; updated_at?: string; last_activity: string
  operator_id?: string; source_chat_id?: string; job_count: number; message_count?: number; latest_job?: Job
}
type SessionDetail = Session & { messages?: TranscriptMessage[]; jobs?: Job[] }
// Sessions created by the Chat view (POST /api/chat) live only in the Chat view.
const isChatSessionId = (id: string) => id.includes(':webchat-') || id.startsWith('webchat-')
const isChatSession = (session: Session) => isChatSessionId(session.id) || (session.source_chat_id || '').startsWith('webchat-')
const taskSessionsOnly = (items: Session[]) => items.filter(item => !isChatSession(item))
type Approval = {
  id: string
  kind?: 'job' | 'tool'
  job_id?: string
  tool_name?: string
  arg?: string
  summary?: string
  session_id?: string
  action?: string
  status: string
  created_at?: string
  expires_at?: number
}
type NodeInfo = {
  id: string; name: string; type: string; status: string; last_seen_at?: string
  capabilities: string[]; metadata?: Record<string, unknown>
}
type SystemStatus = {
  uptime_seconds: number; load_average: number[]; cpu_count: number
  memory: { total: number | null; available: number | null }
  disk: { total: number; used: number; free: number }
  queue: { depth: number; paused: boolean; states: Record<string, number> }
  channels: Record<string, { configured: boolean }>; nodes: NodeInfo[]
  features?: {
    long_term_memory?: boolean
    routines?: boolean
    webhooks?: boolean
    approval_inbox?: boolean
    skills?: boolean
    provider_key_scoping?: boolean
    mobile_ui?: boolean
    mcp?: boolean
    teammate?: boolean
  }
}
type ComputerStatus = {
  armed: boolean; arm_remaining_seconds: number; active_task?: Record<string, unknown> | null
  screenshots: { artifact_id: string; created_at?: string; width?: number; height?: number; node_id?: string }[]
  screen_preview_enabled?: boolean
  screen_request?: {
    request_id: string; status: string; created_at?: string; upload_status?: string | null; error?: string | null
  } | null
}
type ProviderConfig = {
  provider_id: string; provider_name: string; model: string; reasoning_effort: string
  base_url: string; wire_api: 'responses' | 'chat'; env_key: string
  api_key_configured: boolean; api_key_hint: string; config_path: string
}

const statusOrder = ['running', 'queued', 'interrupted', 'failed', 'cancelled', 'completed']

function formatTime(value?: string) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}
function bytes(value: number | null) {
  if (value == null) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']; let n = value; let i = 0
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++ }
  return `${n.toFixed(i > 1 ? 1 : 0)} ${units[i]}`
}
function stateLabel(state?: string) {
  return (state || 'unknown').replace('_', ' ')
}
function sessionLabel(session: Session | undefined) {
  return session?.title || session?.latest_job?.prompt_preview || 'New session'
}
function hostScreenRequestLabel(request: ComputerStatus['screen_request']) {
  if (!request) return ''
  if (request.status === 'pending') return 'Waiting for the Mac agent…'
  if (request.status === 'claimed') return 'Capturing a fresh screenshot on the Mac…'
  if (request.status === 'completed' && ['pending', 'claimed'].includes(request.upload_status || '')) return 'Preparing the private thumbnail…'
  if (request.status === 'completed' && request.upload_status === 'completed') return 'Latest preview is ready'
  if (request.status === 'completed' && request.upload_status === 'failed') return 'Screenshot captured · thumbnail sharing failed'
  if (request.status === 'completed') return 'Captured locally · preview upload unavailable'
  if (request.status === 'failed') return 'Capture failed' + (request.error ? ' · ' + request.error.replaceAll('_', ' ') : '')
  if (request.status === 'expired') return 'Screenshot request expired'
  if (request.status === 'cancelled') return 'Screenshot request cancelled'
  return 'Screenshot · ' + stateLabel(request.status)
}
function hostScreenRequestPending(request: ComputerStatus['screen_request']) {
  return Boolean(request && (
    ['pending', 'claimed'].includes(request.status)
    || (request.status === 'completed' && ['pending', 'claimed'].includes(request.upload_status || ''))
  ))
}

function isEscapeKey(e: Pick<KeyboardEvent, 'key' | 'code' | 'keyCode'>): boolean {
  return e.key === 'Escape' || e.key === 'Esc' || e.code === 'Escape' || e.keyCode === 27
}

export default function App() {
  const [token, setToken] = useState(() => sessionStorage.getItem('conveyor-token') || '')
  const [tokenDraft, setTokenDraft] = useState('')
  const [authenticated, setAuthenticated] = useState(false)
  const [sessions, setSessions] = useState<Session[]>([])
  const [jobs, setJobs] = useState<Job[]>([])
  const [selectedSessionId, setSelectedSessionId] = useState(() => sessionStorage.getItem('conveyor-selected-session') || '')
  const [selectedJobId, setSelectedJobId] = useState('')
  const [creatingSession, setCreatingSession] = useState(false)
  const [transcript, setTranscript] = useState<TranscriptMessage[]>([])
  const [events, setEvents] = useState<EventItem[]>([])
  const [approvals, setApprovals] = useState<Approval[]>([])
  const [nodes, setNodes] = useState<NodeInfo[]>([])
  const [system, setSystem] = useState<SystemStatus | null>(null)
  const [computer, setComputer] = useState<ComputerStatus | null>(null)
  const [diff, setDiff] = useState('')
  const [prompt, setPrompt] = useState('')
  const [mode, setMode] = useState<'run' | 'fix'>('run')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [screenBusy, setScreenBusy] = useState(false)
  const [screenError, setScreenError] = useState('')
  const [providerConfig, setProviderConfig] = useState<ProviderConfig | null>(null)
  const [view, setView] = useState<'tasks' | 'chat' | 'inbox' | 'memory' | 'approvals' | 'skills' | 'connectors' | 'teammate'>(() => (window.location.hash === '#memory' || window.location.pathname === '/memory' ? 'memory' : window.location.hash === '#skills' || window.location.pathname === '/skills' ? 'skills' : window.location.hash === '#connectors' || window.location.pathname === '/connectors' ? 'connectors' : window.location.hash === '#teammate' || window.location.pathname === '/teammate' ? 'teammate' : 'tasks'))
  const [inboxUnread, setInboxUnread] = useState(0)
  const [approvalInboxCount, setApprovalInboxCount] = useState(0)
  const [chatDraft, setChatDraft] = useState('')
  const lastSequence = useRef(0)
  const refreshGen = useRef(0)
  const approvalInboxFetchGen = useRef(0)
  const streamRef = useRef<HTMLDivElement>(null)

  const [moreSheetOpen, setMoreSheetOpen] = useState(false)
  const [sessionsDrawerOpen, setSessionsDrawerOpen] = useState(false)
  const [contextDrawerOpen, setContextDrawerOpen] = useState(false)

  useEffect(() => {
    if (system?.features?.mobile_ui) {
      let link = document.querySelector<HTMLLinkElement>('link[rel="manifest"]')
      if (!link) {
        link = document.createElement('link')
        link.rel = 'manifest'
        link.href = '/manifest.webmanifest'
        document.head.appendChild(link)
      }
    } else {
      const link = document.querySelector<HTMLLinkElement>('link[rel="manifest"]')
      if (link) link.remove()
    }
  }, [system?.features?.mobile_ui])

  const sessionsDrawerRef = useRef<HTMLElement>(null)
  const contextDrawerRef = useRef<HTMLElement>(null)
  const moreButtonRef = useRef<HTMLButtonElement>(null)
  const anyMobileOverlayOpen = moreSheetOpen || sessionsDrawerOpen || contextDrawerOpen

  // Latest overlay state for the always-on listeners below (read through a ref so the
  // listeners never depend on React re-registering them at the right moment).
  const overlayStateRef = useRef({ more: false, sessions: false, context: false })
  overlayStateRef.current = { more: moreSheetOpen, sessions: sessionsDrawerOpen, context: contextDrawerOpen }
  const mobileUiOn = Boolean(system?.features?.mobile_ui)

  // Close drawers / the More sheet on Escape and on any tap or click outside the
  // open drawer. The listeners are registered once (whenever the mobile UI is on),
  // on `window` in the CAPTURE phase, so they run before any element handler,
  // regardless of focus (a focused textarea cannot swallow Esc) and regardless of
  // which input path produced the tap: pointerdown (mouse, pen, touch and DevTools
  // touch emulation), touchend (touch without pointer events) and click (keyboard /
  // synthetic). After closing on pointerdown, the click that follows it is swallowed
  // so the tap does not also activate whatever was underneath the drawer.
  useEffect(() => {
    if (!mobileUiOn) return
    let swallowClicksUntil = 0
    const anyOpen = () => { const s = overlayStateRef.current; return s.more || s.sessions || s.context }
    const openDrawer = () => {
      const s = overlayStateRef.current
      return s.sessions ? sessionsDrawerRef.current : s.context ? contextDrawerRef.current : null
    }
    const closeAll = () => {
      setMoreSheetOpen(false)
      setSessionsDrawerOpen(false)
      setContextDrawerOpen(false)
    }
    const onKeyDown = (e: KeyboardEvent) => {
      if (!anyOpen() || !isEscapeKey(e)) return
      e.preventDefault()
      e.stopPropagation()
      closeAll()
    }
    const onTap = (e: Event) => {
      if (e.type === 'click' && Date.now() < swallowClicksUntil) {
        swallowClicksUntil = 0
        e.preventDefault()
        e.stopPropagation()
        return
      }
      const drawer = openDrawer()
      if (!drawer) return // the More sheet handles its own overlay click
      const target = e.target as Node | null
      if (!target || drawer.contains(target)) return
      if (e.cancelable) e.preventDefault()
      e.stopPropagation()
      if (e.type !== 'click') swallowClicksUntil = Date.now() + 800
      closeAll()
    }
    const tapEvents = ['pointerdown', 'touchend', 'click'] as const
    window.addEventListener('keydown', onKeyDown, true)
    tapEvents.forEach(type => window.addEventListener(type, onTap, { capture: true, passive: false }))
    return () => {
      window.removeEventListener('keydown', onKeyDown, true)
      tapEvents.forEach(type => window.removeEventListener(type, onTap, { capture: true }))
    }
  }, [mobileUiOn])

  // While a drawer or the sheet is open, pin the page zoom to 1: fixed overlays are
  // laid out against the layout viewport, so a pinch-zoomed / panned visual viewport
  // (e.g. DevTools device mode after a Shift-drag or trackpad pinch) can hide the
  // backdrop strip and part of the drawer off-screen. Clamping maximum-scale resets the
  // zoom; the original viewport (user zoom allowed) is restored on close.
  useEffect(() => {
    if (!mobileUiOn || !anyMobileOverlayOpen) return
    const meta = document.querySelector('meta[name="viewport"]')
    if (!meta) return
    const previous = meta.getAttribute('content') || 'width=device-width, initial-scale=1.0'
    meta.setAttribute('content', 'width=device-width, initial-scale=1.0, maximum-scale=1.0')
    return () => meta.setAttribute('content', previous)
  }, [mobileUiOn, anyMobileOverlayOpen])

  // Move focus into an opened drawer so keyboard users (and Esc) target the page;
  // when it closes, hand focus back to the More button instead of the hidden drawer.
  useEffect(() => {
    const drawer = sessionsDrawerOpen ? sessionsDrawerRef.current : contextDrawerOpen ? contextDrawerRef.current : null
    if (drawer) {
      drawer.focus({ preventScroll: true })
      return
    }
    const active = document.activeElement
    if (active && (sessionsDrawerRef.current?.contains(active) || contextDrawerRef.current?.contains(active))) {
      moreButtonRef.current?.focus({ preventScroll: true })
    }
  }, [sessionsDrawerOpen, contextDrawerOpen])

  const selectSession = useCallback((sessionId: string, jobId?: string) => {
    setCreatingSession(false)
    setSelectedSessionId(sessionId)
    if (sessionId) sessionStorage.setItem('conveyor-selected-session', sessionId)
    else sessionStorage.removeItem('conveyor-selected-session')
    setSelectedJobId(jobId || '')
    setTranscript([])
  }, [])

  const api = useCallback(async <T,>(path: string, init?: RequestInit): Promise<T> => {
    const response = await fetch(path, {
      ...init,
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}`, ...(init?.headers || {}) },
    })
    if (response.status === 401) { setAuthenticated(false); throw new Error('Token rejected') }
    const body = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(body.error || body.message || `Request failed (${response.status})`)
    return body as T
  }, [token])

  const archiveSession = useCallback(async (sessionId: string) => {
    try {
      await api(`/api/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' })
      if (selectedSessionId === sessionId) {
        sessionStorage.removeItem('conveyor-selected-session')
        setSelectedSessionId('')
        setSelectedJobId('')
        setTranscript([])
      }
      void api<{ sessions: Session[] }>('/api/sessions').then(data => {
        const taskSessions = taskSessionsOnly(data.sessions)
        setSessions(taskSessions)
        if (selectedSessionId === sessionId && taskSessions[0]) {
          selectSession(taskSessions[0].id, taskSessions[0].latest_job?.id)
        }
      })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not archive session')
    }
  }, [api, selectSession, selectedSessionId])

  const refresh = useCallback(async () => {
    if (!token) return
    const gen = ++refreshGen.current
    try {
      const [sessionData, jobData, approvalData, nodeData, systemData, computerData] = await Promise.all([
        api<{ sessions: Session[] }>('/api/sessions'), api<{ jobs: Job[] }>('/api/jobs'),
        api<{ approvals: Approval[] }>('/api/approvals'), api<{ nodes: NodeInfo[] }>('/api/nodes'),
        api<SystemStatus>('/api/system/status'), api<ComputerStatus>('/api/computer/status'),
      ])
      // A newer refresh (or a decision that bumped the generation) started
      // while this one was in flight. Drop it so a late poll cannot restore
      // a pending approval or an old job state.
      if (isStale(gen, refreshGen.current)) return
      const taskSessions = taskSessionsOnly(sessionData.sessions)
      setSessions(taskSessions); setJobs(jobData.jobs); setApprovals(approvalData.approvals)
      setNodes(nodeData.nodes); setSystem(systemData); setComputer(computerData); setAuthenticated(true); setError('')
      if (!creatingSession) {
        const savedSessionId = sessionStorage.getItem('conveyor-selected-session')
        const currentTargetId = selectedSessionId || savedSessionId
        const matched = taskSessions.find(item => item.id === currentTargetId)
        if (matched) {
          if (selectedSessionId !== matched.id) {
            setSelectedSessionId(matched.id)
            setSelectedJobId(matched.latest_job?.id || '')
          } else if (!selectedJobId && matched.latest_job) {
            setSelectedJobId(matched.latest_job.id)
          }
        } else if (!selectedSessionId || isChatSessionId(selectedSessionId)) {
          const initial = taskSessions[0]
          if (initial) {
            setSelectedSessionId(initial.id)
            setSelectedJobId(initial.latest_job?.id || '')
            sessionStorage.setItem('conveyor-selected-session', initial.id)
          } else if (selectedSessionId) {
            setSelectedSessionId('')
            setSelectedJobId('')
            sessionStorage.removeItem('conveyor-selected-session')
          }
        }
      }
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Could not connect') }
  }, [api, creatingSession, selectedJobId, selectedSessionId, token])

  const refreshTranscript = useCallback(async () => {
    if (!authenticated || !selectedSessionId) { setTranscript([]); return }
    try {
      const session = await api<SessionDetail>(`/api/sessions/${encodeURIComponent(selectedSessionId)}`)
      setTranscript(session.messages || [])
    } catch { setTranscript([]) }
  }, [api, authenticated, selectedSessionId])

  useEffect(() => { void refresh() }, [refresh])
  useEffect(() => {
    if (!authenticated) return
    const timer = window.setInterval(() => { void refresh(); void refreshTranscript() }, 3_000)
    return () => window.clearInterval(timer)
  }, [authenticated, refresh, refreshTranscript])

  useEffect(() => {
    void refreshTranscript()
  }, [refreshTranscript])

  const fetchApprovalInboxCount = useCallback(async () => {
    if (!system?.features?.approval_inbox || !authenticated) return
    const gen = ++approvalInboxFetchGen.current
    try {
      const res = await fetch('/api/approval-inbox', {
        headers: { Authorization: `Bearer ${token}` },
      })
      if (isStale(gen, approvalInboxFetchGen.current)) return
      if (!res.ok) return
      const data = await res.json()
      if (isStale(gen, approvalInboxFetchGen.current)) return
      setApprovalInboxCount(data.counts?.total ?? data.items?.length ?? 0)
    } catch {}
  }, [system?.features?.approval_inbox, authenticated, token])

  // Count reported by the Approvals panel (fresh after a decision): drop any
  // badge poll that started earlier so it cannot overwrite the new value.
  const setApprovalInboxCountFresh = useCallback((count: number) => {
    approvalInboxFetchGen.current += 1
    setApprovalInboxCount(count)
  }, [])

  useEffect(() => {
    if (!system?.features?.approval_inbox || !authenticated) return
    void fetchApprovalInboxCount()
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') {
        void fetchApprovalInboxCount()
      }
    }, 5_000)
    return () => window.clearInterval(timer)
  }, [fetchApprovalInboxCount, system?.features?.approval_inbox, authenticated])

  useEffect(() => {
    if (!authenticated || !selectedJobId) return
    let stopped = false; let controller: AbortController | null = null; let retry: number | undefined
    lastSequence.current = 0; setEvents([]); setDiff('')
    void api<{ events: EventItem[] }>(`/api/jobs/${selectedJobId}/events`).then(({ events: initial }) => {
      if (stopped) return
      const unique = [...new Map(initial.map(item => [item.event_id, item])).values()]
      setEvents(unique.slice(-1000)); lastSequence.current = unique.at(-1)?.sequence || 0
    }).catch(reason => setError(String(reason)))
    void api<{ diff: string }>(`/api/jobs/${selectedJobId}/diff`).then(data => !stopped && setDiff(data.diff)).catch(() => {})

    const connect = async () => {
      controller = new AbortController()
      try {
        const response = await fetch(`/api/events/stream?job_id=${encodeURIComponent(selectedJobId)}&after=${lastSequence.current}`, {
          headers: { Authorization: `Bearer ${token}` }, signal: controller.signal,
        })
        if (!response.ok || !response.body) throw new Error('Realtime unavailable')
        const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = ''
        while (!stopped) {
          const { value, done } = await reader.read(); if (done) break
          buffer += decoder.decode(value, { stream: true })
          const frames = buffer.split('\n\n'); buffer = frames.pop() || ''
          for (const frame of frames) {
            const line = frame.split('\n').find(part => part.startsWith('data: ')); if (!line) continue
            const event = JSON.parse(line.slice(6)) as EventItem
            lastSequence.current = Math.max(lastSequence.current, event.sequence)
            setEvents(previous => previous.some(item => item.event_id === event.event_id) ? previous : [...previous, event].slice(-1000))
            if (shouldRefreshForEvent(event.kind)) {
              void refresh()
              void refreshTranscript()
            }
          }
        }
      } catch { /* reconnect below unless the selection changed */ }
      if (!stopped) retry = window.setTimeout(() => void connect(), 1500)
    }
    void connect()
    return () => { stopped = true; controller?.abort(); if (retry) window.clearTimeout(retry) }
  }, [api, authenticated, refresh, refreshTranscript, selectedJobId, token])

  const selectedJob = useMemo(() => jobs.find(job => job.id === selectedJobId), [jobs, selectedJobId])
  const selectedSession = useMemo(() => sessions.find(session => session.id === selectedSessionId), [sessions, selectedSessionId])
  useEffect(() => {
    if (!selectedJob || creatingSession) return
    const matching = sessions.find(session => session.channel === selectedJob.channel
      && session.operator_id === selectedJob.operator_id
      && session.source_chat_id === selectedJob.chat_id)
    const target = matching?.id || selectedJob.chat_id
    if (target && target !== selectedSessionId) setSelectedSessionId(target)
  }, [creatingSession, selectedJob, selectedSessionId, sessions])
  const pendingForJob = approvals.filter(item => (!item.kind || item.kind === 'job') && Boolean(item.job_id && item.job_id === selectedJobId))
  const pendingToolApprovals = approvals.filter(item => item.kind === 'tool' && !isChatSessionId(item.session_id || '') && (!selectedSessionId || item.session_id === selectedSessionId))
  const runtimeOwner = runtimeOwnerFromJob(selectedJob)
  const toolEvents = useMemo(() => events.filter(item => item.kind.startsWith('tool.')), [events])
  const refinementTurn = Number(selectedJob?.metadata?.refinement_turn || 0)
  const refinementClosed = useMemo(() => events.some(item => item.kind === 'refinement.closed'), [events])
  const activeRefinement = Boolean(selectedJob?.metadata?.refinement_chain_id && !refinementClosed)
  const activeChangedFiles = selectedJob?.changed_files?.length || 0
  const liveAssistantText = useMemo(() => {
    if (!selectedJob || terminalJobState(selectedJob.state)) return ''
    return events
      .filter(item => item.kind === 'assistant.delta')
      .map(item => String(item.payload.text || ''))
      .join('')
  }, [events, selectedJob])
  const promptAlreadyInTranscript = useMemo(() => {
    if (!selectedJob) return true
    return transcript.some(m =>
      m.job_id === selectedJob.id ||
      (m.role === 'user' && m.content.trim() === selectedJob.prompt_preview.trim())
    )
  }, [selectedJob, transcript])

  useEffect(() => {
    const node = streamRef.current
    if (node) node.scrollTo({ top: node.scrollHeight, behavior: 'smooth' })
  }, [events.length, transcript.length, selectedJobId])

  async function openSettings() {
    setSettingsOpen(true); setError('')
    try { setProviderConfig(await api<ProviderConfig>('/api/config/provider')) }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Could not load settings') }
  }

  async function submit(event: FormEvent) {
    event.preventDefault(); if (!prompt.trim() || busy) return
    setBusy(true); setError('')
    try {
      const result = await api<{ job_id: string; session_id?: string }>('/api/tasks', {
        method: 'POST', body: JSON.stringify({ prompt: prompt.trim(), mode, session_id: selectedSessionId || undefined }),
      })
      setPrompt('')
      setCreatingSession(false)
      if (result.session_id) setSelectedSessionId(result.session_id)
      setSelectedJobId(result.job_id || '')
      await Promise.all([refresh(), refreshTranscript()])
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Submit failed') }
    finally { setBusy(false) }
  }
  async function action(path: string, body: object = {}) {
    setBusy(true); setError('')
    const decided = path.match(/^\/api\/approvals\/([^/]+)\/(approve|reject)$/)
    if (decided) {
      const approvalId = decodeURIComponent(decided[1])
      setApprovals(prev => dropApproval(prev, approvalId))
    }
    // Invalidate a poll that started before this decision.
    refreshGen.current += 1
    try { await api(path, { method: 'POST', body: JSON.stringify(body) }); await refresh(); if (decided) void fetchApprovalInboxCount() }
    catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Action failed')
      // The optimistic drop may have been wrong (network error). Re-read.
      try { await refresh() } catch { /* status already reported */ }
    }
    finally { setBusy(false) }
  }
  async function captureHostScreen() {
    if (screenBusy || !computer?.screen_preview_enabled) return
    setScreenBusy(true); setScreenError('')
    try {
      const result = await api<{ request: NonNullable<ComputerStatus['screen_request']> }>('/api/computer/screenshot', {
        method: 'POST', body: JSON.stringify({}),
      })
      setComputer(previous => previous ? { ...previous, screen_request: result.request } : previous)
      await refresh()
    } catch (reason) {
      setScreenError(reason instanceof Error ? reason.message : 'Could not request a host screenshot')
    } finally { setScreenBusy(false) }
  }
  function unlock(event: FormEvent) {
    event.preventDefault(); const value = tokenDraft.trim(); if (!value) return
    sessionStorage.setItem('conveyor-token', value); setToken(value); setTokenDraft('')
  }

  if (!authenticated) return <main className="unlock-shell">
    <form className="unlock-card" onSubmit={unlock}>
      <div className="brand-mark">C</div><p className="eyebrow">SECURE CONTROL PLANE</p>
      <h1>Open Conveyor</h1><p>Enter the bearer token configured on your VPS. It stays in this browser tab only.</p>
      <label>Console token<input type="password" autoFocus value={tokenDraft} onChange={event => setTokenDraft(event.target.value)} placeholder="32+ character token" /></label>
      {error && <div className="error-banner">{error}</div>}<button className="primary" type="submit">Unlock console</button>
    </form>
  </main>

  return <main className={`app-shell ${system?.features?.mobile_ui ? 'mobile-ui' : ''}`}>
    <header className="topbar">
      <div className="brand"><span className="brand-mark small">C</span><div><strong>Conveyor</strong><small>CONTROL CONSOLE</small></div></div>
      <div className="top-actions"><button className="settings-button" onClick={() => void openSettings()}>⚙ Settings</button><div className="top-status"><span className="live-dot" /> Online <span className="separator" /> Queue {system?.queue.depth ?? 0}</div></div>
    </header>
    {error && <div className="error-banner global">{error}<button onClick={() => setError('')}>×</button></div>}
    <section className="workspace">
      <aside ref={sessionsDrawerRef} tabIndex={sessionsDrawerOpen ? -1 : undefined} role={sessionsDrawerOpen ? 'dialog' : undefined} aria-modal={sessionsDrawerOpen ? true : undefined} aria-label={sessionsDrawerOpen ? 'Sessions' : undefined} className={`sessions-panel panel ${sessionsDrawerOpen ? 'drawer-open' : ''}`}>
        <div className="mobile-drawer-header">
          <strong>Sessions</strong>
          <button type="button" className="drawer-close-btn" aria-label="Close sessions" onClick={() => setSessionsDrawerOpen(false)}>×</button>
        </div>
        <div className="panel-heading"><div><p className="eyebrow">WORKSPACES</p><h2>Sessions</h2></div><button className="icon-button" onClick={() => { selectSession('', ''); setCreatingSession(true); setPrompt(''); setSessionsDrawerOpen(false); }} aria-label="New session">＋</button></div>
        <div className="session-list">
          {sessions.map(session => (
            <div key={session.id} className="session-item-row">
              <button
                className={`session-item ${session.id === selectedSessionId ? 'active' : ''}`}
                title={sessionLabel(session)}
                aria-pressed={session.id === selectedSessionId}
                onClick={() => { selectSession(session.id, session.latest_job?.id); setSessionsDrawerOpen(false); }}
              >
                <span className={`status-rail ${session.latest_job?.state || ''}`} />
                <span>
                  <strong>{session.title || 'Untitled session'}</strong>
                  <small>{session.message_count ?? 0} messages · {session.job_count} job{session.job_count === 1 ? '' : 's'} · {formatTime(session.last_activity)}</small>
                </span>
              </button>
              <button
                type="button"
                className="session-archive-btn"
                title="Archive session"
                aria-label="Archive session"
                onClick={(e) => { e.stopPropagation(); void archiveSession(session.id) }}
              >
                ×
              </button>
            </div>
          ))}
          {!sessions.length && <Empty text="No sessions yet" />}
        </div>
        <div className="queue-summary"><p className="eyebrow">ACTIVE QUEUE</p>{(['running', 'queued'] as const).map(state => <div key={state}><span>{state}</span><strong>{system?.queue.states[state] || 0}</strong></div>)}<p className="history-note">History · {(['interrupted', 'failed', 'cancelled', 'completed'] as const).reduce((total, state) => total + (system?.queue.states[state] || 0), 0)} terminal tasks</p></div>
      </aside>

      <section className="stream-panel panel">
        <div className="stream-header">
          <div>
            <p className="eyebrow">
              {view === 'chat' ? 'DIRECT CHAT TIER' : view === 'inbox' ? 'ROUTINES · INBOX' : view === 'memory' ? 'LONG-TERM MEMORY' : view === 'approvals' ? 'UNIFIED APPROVAL INBOX' : view === 'skills' ? 'SKILLS LIBRARY' : view === 'connectors' ? 'MCP CONNECTORS' : view === 'teammate' ? 'ALWAYS-ON TEAMMATE · 24/7 SENTRY' : 'TASKS · CODEX EXECUTION'}
            </p>
            <h2>
              {view === 'chat' ? 'Chat' : view === 'inbox' ? 'Inbox & Routines' : view === 'memory' ? 'Memory' : view === 'approvals' ? 'Approvals' : view === 'skills' ? 'Skills' : view === 'connectors' ? 'Connectors' : view === 'teammate' ? 'Teammate' : (creatingSession ? 'New session' : sessionLabel(selectedSession))}
            </h2>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <div className="mode-switch">
              <button type="button" className={view === 'tasks' ? 'active' : ''} onClick={() => setView('tasks')}>Tasks</button>
              <button type="button" className={view === 'chat' ? 'active' : ''} onClick={() => setView('chat')}>Chat</button>
              <button type="button" className={view === 'inbox' ? 'active' : ''} onClick={() => setView('inbox')}>
                Inbox{inboxUnread > 0 ? ` (${inboxUnread})` : ''}
              </button>
              {(system?.features?.long_term_memory || view === 'memory') && (
                <button type="button" className={view === 'memory' ? 'active' : ''} onClick={() => setView('memory')}>Memory</button>
              )}
              {Boolean(system?.features?.approval_inbox) && (
                <button type="button" className={view === 'approvals' ? 'active' : ''} onClick={() => setView('approvals')}>
                  Approvals{approvalInboxCount > 0 ? ` (${approvalInboxCount})` : ''}
                </button>
              )}
              {(Boolean(system?.features?.skills) || view === 'skills') && (
                <button type="button" className={view === 'skills' ? 'active' : ''} onClick={() => setView('skills')}>Skills</button>
              )}
              {(Boolean(system?.features?.mcp) || view === 'connectors') && (
                <button type="button" className={view === 'connectors' ? 'active' : ''} onClick={() => setView('connectors')}>Connectors</button>
              )}
              {(Boolean(system?.features?.teammate ?? true) || view === 'teammate') && (
                <button type="button" className={view === 'teammate' ? 'active' : ''} onClick={() => setView('teammate')}>Teammate</button>
              )}
            </div>
            {view === 'tasks' && selectedJob && <StatusBadge state={selectedJob.state} />}
          </div>
        </div>
        {view === 'approvals' ? (
          <ApprovalInboxPanel
            token={token}
            onApprovalDecided={() => {
              void refresh()
              void fetchApprovalInboxCount()
            }}
            onPendingCountChange={setApprovalInboxCountFresh}
          />
        ) : view === 'skills' ? (
          <SkillsPanel
            token={token}
            onUseInChat={(skillSlug) => {
              setChatDraft(`/skill ${skillSlug} `)
              setView('chat')
            }}
          />
        ) : view === 'connectors' ? (
          <ConnectorsPanel token={token} />
        ) : view === 'memory' ? (
          <MemoryPanel token={token} />
        ) : view === 'inbox' ? (
          <InboxPanel
            token={token}
            webhooksEnabled={Boolean(system?.features?.webhooks)}
            onUnreadChange={setInboxUnread}
            onApprovalDecided={() => {
              void refresh()
              void fetchApprovalInboxCount()
            }}
          />
        ) : view === 'chat' ? (
          <ChatPanel
            token={token}
            onApprovalDecided={() => {
              void refresh()
              void fetchApprovalInboxCount()
            }}
            onSessionChange={refresh}
            initialInput={chatDraft}
            onInitialInputConsumed={() => setChatDraft('')}
          />
        ) : view === 'teammate' ? (
          <TeammatePanel
            token={token}
            onSendToChat={(cmd) => {
              setChatDraft(cmd)
              setView('chat')
            }}
          />
        ) : (
          <>
            <div className="event-stream" ref={streamRef}>
              {activeRefinement && <div className="job-notice"><strong>Active changes</strong><span>{refinementTurn || 1} refinement turn{(refinementTurn || 1) === 1 ? '' : 's'} · {activeChangedFiles} file{activeChangedFiles === 1 ? '' : 's'} changed · Fix feedback continues the same worktree.</span></div>}
              {!transcript.length && selectedJob?.state === 'failed' && <div className="job-notice failed"><strong>Task failed</strong><span>{selectedJob.error || 'See the execution details below.'}</span></div>}
              {!transcript.length && selectedJob?.state === 'cancelled' && <div className="job-notice"><strong>Task cancelled</strong><span>This task was cancelled; start a new message to continue.</span></div>}

              {transcript.length > 0 && <TranscriptPanel messages={transcript} />}

              {selectedJob && !terminalJobState(selectedJob.state) && !promptAlreadyInTranscript && (
                <article className="transcript-message role-user pending-turn">
                  <div className="transcript-content">
                    <p className="formatted-paragraph">{selectedJob.prompt_preview}</p>
                  </div>
                </article>
              )}

              {selectedJob && !terminalJobState(selectedJob.state) && (
                <div className="live-job-turn">
                  <div className={`live-job-banner ${selectedJob.state}`}>
                    <span className={`live-pulse ${selectedJob.state}`} />
                    <span>
                      {selectedJob.state === 'queued'
                        ? `排队中 · 等待执行 (${selectedJob.id})…`
                        : `Conveyor 正在执行 (${selectedJob.id})…`}
                    </span>
                  </div>

                  {toolEvents.length > 0 && (
                    <div className="live-tools-list">
                      {toolEvents.map(tool => (
                        <EventCard key={tool.event_id} item={tool} />
                      ))}
                    </div>
                  )}

                  {liveAssistantText && (
                    <article className="transcript-message role-assistant live-streaming">
                      <div className="transcript-avatar" aria-hidden="true">🤖</div>
                      <div className="transcript-body">
                        <div className="transcript-header" style={{ marginBottom: 4 }}>
                          <span className="streaming-indicator">● 生成中</span>
                        </div>
                        <div className="transcript-content">
                          <FormattedText content={liveAssistantText} />
                          <span className="typing-cursor">▌</span>
                        </div>
                      </div>
                    </article>
                  )}
                </div>
              )}

              {selectedJob && terminalJobState(selectedJob.state) && toolEvents.length > 0 && (
                <details className="completed-tools-drawer">
                  <summary className="completed-tools-summary">
                    <span className="tool-summary-icon">⚡</span>
                    <span>
                      <strong>{toolEvents.length} 个工具操作</strong> 已在任务 {selectedJob.id} 中执行
                    </span>
                    <span className="expand-hint">点击查看明细 ⌄</span>
                  </summary>
                  <div className="completed-tools-body">
                    {toolEvents.map(tool => (
                      <EventCard key={tool.event_id} item={tool} />
                    ))}
                  </div>
                </details>
              )}

              {!transcript.length && (!selectedJob || terminalJobState(selectedJob.state)) && (
                <div className="welcome-state">
                  <div className="brand-mark">C</div>
                  <h2>Conveyor Control Console</h2>
                  <p>你的个人常驻 Agent 控制台。在下方输入任务，或通过 Telegram、飞书随时交流。</p>
                  <div className="suggested-prompts">
                    <button type="button" className="prompt-chip" onClick={() => setPrompt('检查当前系统状态和任务队列')}>
                      🔍 检查系统与任务队列
                    </button>
                    <button type="button" className="prompt-chip" onClick={() => setPrompt('查看最近的代码修改和 Git 提交记录')}>
                      🛠️ 查看最近代码变更
                    </button>
                    <button type="button" className="prompt-chip" onClick={() => setPrompt('帮我梳理今天的工作进展和待办事项')}>
                      📝 总结今日工作进展
                    </button>
                  </div>
                </div>
              )}
            </div>
            <form className="composer" onSubmit={submit}>
              <div className="mode-switch"><button type="button" className={mode === 'run' ? 'active' : ''} onClick={() => setMode('run')}>Ask</button><button type="button" className={mode === 'fix' ? 'active' : ''} onClick={() => setMode('fix')}>Fix</button></div>
              <textarea value={prompt} onChange={event => setPrompt(event.target.value)} placeholder={activeRefinement ? 'Refine the active changes…' : 'Ask Conveyor…'} rows={2} maxLength={8000} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit() } }} />
              <button className="send-button" disabled={!prompt.trim() || busy}>{busy ? '…' : 'Send'} <span>↗</span></button>
            </form>
          </>
        )}
      </section>

      <aside ref={contextDrawerRef} tabIndex={contextDrawerOpen ? -1 : undefined} role={contextDrawerOpen ? 'dialog' : undefined} aria-modal={contextDrawerOpen ? true : undefined} aria-label={contextDrawerOpen ? 'Context and changes' : undefined} className={`context-panel panel ${contextDrawerOpen ? 'drawer-open' : ''}`}>
        <div className="mobile-drawer-header">
          <strong>Context & Changes</strong>
          <button type="button" className="drawer-close-btn" aria-label="Close context" onClick={() => setContextDrawerOpen(false)}>×</button>
        </div>
        <ContextSection title="Job">
          {selectedJob ? <>
            <KeyValue label="ID" value={selectedJob.id} mono /><KeyValue label="State" value={selectedJob.state} />
            <KeyValue label="Provider" value="Codex" /><KeyValue label="Mode" value={selectedJob.mode} />
            <KeyValue label="Started" value={formatTime(selectedJob.started_at)} />
            {activeRefinement && <KeyValue label="Refinement" value={`${refinementTurn || 1} turn${(refinementTurn || 1) === 1 ? '' : 's'} · active`} />}
            <RuntimeOwnerCard owner={runtimeOwner} state={selectedJob.state} />
            <div className="action-row"><button disabled={busy || !['queued','running'].includes(selectedJob.state)} onClick={() => action(`/api/jobs/${selectedJob.id}/cancel`)}>Cancel</button></div>
            {events.length > 0 && (
              <details className="telemetry-drawer">
                <summary className="eyebrow telemetry-summary">
                  <span>TELEMETRY ({events.length})</span>
                  <b>⌄</b>
                </summary>
                <div className="telemetry-list">
                  {events.map(ev => (
                    <div key={ev.event_id} className="telemetry-item">
                      <div className="telemetry-item-header">
                        <span className={`telemetry-pill ${ev.kind.split('.')[0]}`}>{ev.kind}</span>
                        <time>{formatTime(ev.timestamp)}</time>
                      </div>
                      {Boolean(ev.payload?.text) && <div className="telemetry-text">{String(ev.payload.text)}</div>}
                      {Boolean(ev.payload?.name) && <div className="telemetry-text">Tool: {String(ev.payload.name)}</div>}
                      {Boolean(ev.payload?.prompt) && <div className="telemetry-text">Prompt: {String(ev.payload.prompt).slice(0, 100)}</div>}
                      {Boolean(ev.payload?.error) && <div className="telemetry-error">{String(ev.payload.error)}</div>}
                    </div>
                  ))}
                </div>
              </details>
            )}
          </> : <Empty text="Select a job" />}
        </ContextSection>
        {pendingForJob.map(approval => <section className="approval-card" key={approval.id}><p className="eyebrow">APPROVAL REQUIRED</p><h3>{approval.action === 'apply' ? 'Apply changes' : 'Discard worktree'}?</h3><p>This decision is scoped to job <code>{approval.job_id}</code> and expires automatically.</p><div className="action-row"><button className="danger" onClick={() => action(`/api/approvals/${approval.id}/reject`)}>Reject</button><button className="primary" onClick={() => action(`/api/approvals/${approval.id}/approve`)}>Approve</button></div></section>)}
        {pendingToolApprovals.map(approval => (
          <section className="approval-card" key={approval.id}>
            <p className="eyebrow">TOOL APPROVAL REQUIRED</p>
            <h3>Execute tool <code>{approval.tool_name}</code>?</h3>
            <p>{approval.summary || approval.arg || 'Tool confirmation required'}</p>
            {approval.arg && <p style={{ fontFamily: 'ui-monospace, monospace', fontSize: 11 }}>Target: {approval.arg}</p>}
            <div className="action-row">
              <button className="danger" onClick={() => action(`/api/approvals/${approval.id}/reject`)}>Reject</button>
              <button className="primary" onClick={() => action(`/api/approvals/${approval.id}/approve`)}>Approve</button>
            </div>
          </section>
        ))}
        <ContextSection title="Changes" className="context-section--changes" collapsible storageKey="conveyor-changes-collapsed">
          {activeRefinement && <KeyValue label="Active changes" value={`${activeChangedFiles} file${activeChangedFiles === 1 ? '' : 's'} · cumulative`} />}
          <div className="file-list">{selectedJob?.changed_files?.map(file => <div key={file.path}><span className="file-status">{file.status || 'M'}</span><code>{file.path}</code></div>)}{selectedJob && !selectedJob.changed_files?.length && <Empty text="No changed files" />}</div>
          {selectedJob && <><details className="diff-view"><summary>Unified diff</summary><pre>{diff || 'No diff available.'}</pre></details><div className="action-row"><button className="danger" disabled={busy} onClick={() => action(`/api/jobs/${selectedJob.id}/discard`)}>{activeRefinement ? 'Discard active changes…' : 'Discard…'}</button><button className="primary" disabled={busy} onClick={() => action(`/api/jobs/${selectedJob.id}/apply`)}>{activeRefinement ? 'Apply active changes…' : 'Apply…'}</button></div></>}
        </ContextSection>
        <ContextSection title="Computer">
          <KeyValue label="CUA" value={computer?.armed ? `Armed · ${computer.arm_remaining_seconds}s` : 'Disarmed'} />
          {computer?.active_task && <KeyValue label="Task" value={String(computer.active_task.status || computer.active_task.task_id || 'active')} />}
          <div className="host-screen-card">
            <div className="host-screen-heading">
              <div><strong>Host screen</strong><small>Read-only · one-shot capture</small></div>
              <button type="button" className="screen-capture-button" disabled={!computer?.screen_preview_enabled || screenBusy || hostScreenRequestPending(computer?.screen_request)} onClick={() => void captureHostScreen()}>
                {screenBusy ? 'Requesting…' : hostScreenRequestPending(computer?.screen_request) ? 'Capturing…' : 'Capture'}
              </button>
            </div>
            {computer?.screenshots[0]
              ? <AuthenticatedImage artifact={computer.screenshots[0]} token={token} />
              : <div className="screen-empty"><span aria-hidden="true">▣</span><strong>No shared screen preview</strong><p>When enabled, an explicit capture sends a size-limited thumbnail. The Mac keeps the original.</p></div>}
            {!computer?.screen_preview_enabled && <p className="screen-privacy-note">Thumbnail sharing is off. Screens stay on the Mac.</p>}
            {computer?.screen_request && <p className={`screen-request-status ${computer.screen_request.status === 'failed' ? 'failed' : ''}`} aria-live="polite">{hostScreenRequestLabel(computer.screen_request)}</p>}
            {screenError && <p className="screen-request-status failed" role="alert">{screenError}</p>}
          </div>
          {nodes.map(node => <div className="node-card" key={node.id}><div><span className={`node-dot ${node.status}`} /><strong>{node.name}</strong></div><small>{node.type} · {node.status}<br />Last seen {formatTime(node.last_seen_at)}</small></div>)}
          {!nodes.length && <Empty text="No execution nodes" />}
          <button className="emergency" onClick={() => action('/api/computer/stop')}>■ Emergency stop</button>
        </ContextSection>
        <ContextSection title="System">
          <KeyValue label="Load" value={system?.load_average.slice(0, 2).map(n => n.toFixed(2)).join(' / ') || '—'} />
          <KeyValue label="Memory free" value={bytes(system?.memory.available ?? null)} /><KeyValue label="Disk free" value={bytes(system?.disk.free ?? null)} />
          <KeyValue label="Telegram" value={system?.channels.telegram.configured ? 'Configured' : 'Off'} /><KeyValue label="Feishu" value={system?.channels.feishu.configured ? 'Configured' : 'Off'} />
        </ContextSection>
      </aside>
    </section>
    {Boolean(system?.features?.mobile_ui) && sessionsDrawerOpen && (
      <div className="mobile-backdrop" role="button" aria-label="Close sessions panel" onClick={() => setSessionsDrawerOpen(false)} />
    )}
    {Boolean(system?.features?.mobile_ui) && contextDrawerOpen && (
      <div className="mobile-backdrop" role="button" aria-label="Close context panel" onClick={() => setContextDrawerOpen(false)} />
    )}
    {Boolean(system?.features?.mobile_ui) && moreSheetOpen && (
      <div className="mobile-sheet-overlay" onClick={() => setMoreSheetOpen(false)}>
        <div className="mobile-sheet" onClick={e => e.stopPropagation()} role="dialog" aria-label="More options">
          <div className="mobile-sheet-header">
            <strong>More views & tools</strong>
            <button type="button" className="drawer-close-btn" aria-label="Close more menu" onClick={() => setMoreSheetOpen(false)}>×</button>
          </div>
          <div className="mobile-sheet-items">
            {(Boolean(system?.features?.routines) || view === 'inbox') && (
              <button
                type="button"
                className={`mobile-sheet-item ${view === 'inbox' ? 'active' : ''}`}
                onClick={() => { setView('inbox'); setMoreSheetOpen(false); }}
              >
                <span>📬 Inbox & Routines</span>
                {inboxUnread > 0 && <span className="mobile-badge">{inboxUnread}</span>}
              </button>
            )}
            {(Boolean(system?.features?.long_term_memory) || view === 'memory') && (
              <button
                type="button"
                className={`mobile-sheet-item ${view === 'memory' ? 'active' : ''}`}
                onClick={() => { setView('memory'); setMoreSheetOpen(false); }}
              >
                <span>🧠 Long-term Memory</span>
              </button>
            )}
            {(Boolean(system?.features?.mcp) || view === 'connectors') && (
              <button
                type="button"
                className={`mobile-sheet-item ${view === 'connectors' ? 'active' : ''}`}
                onClick={() => { setView('connectors'); setMoreSheetOpen(false); }}
              >
                <span>🔌 Connectors</span>
              </button>
            )}
            <button
              type="button"
              className="mobile-sheet-item"
              onClick={() => { setSessionsDrawerOpen(true); setMoreSheetOpen(false); }}
            >
              <span>📑 Sessions</span>
            </button>
            <button
              type="button"
              className="mobile-sheet-item"
              onClick={() => { setContextDrawerOpen(true); setMoreSheetOpen(false); }}
            >
              <span>🔍 Context & Changes</span>
            </button>
            <button
              type="button"
              className="mobile-sheet-item"
              onClick={() => { void openSettings(); setMoreSheetOpen(false); }}
            >
              <span>⚙ Settings</span>
            </button>
          </div>
        </div>
      </div>
    )}
    {Boolean(system?.features?.mobile_ui) && (
      <nav className="mobile-bottom-nav" aria-label="Mobile navigation">
        <button
          type="button"
          className={`mobile-nav-item ${view === 'tasks' && !moreSheetOpen ? 'active' : ''}`}
          onClick={() => { setView('tasks'); setMoreSheetOpen(false); }}
        >
          <span className="mobile-nav-icon">⚡</span>
          <span>Tasks</span>
        </button>

        <button
          type="button"
          className={`mobile-nav-item ${view === 'chat' && !moreSheetOpen ? 'active' : ''}`}
          onClick={() => { setView('chat'); setMoreSheetOpen(false); }}
        >
          <span className="mobile-nav-icon">💬</span>
          <span>Chat</span>
        </button>

        {Boolean(system?.features?.approval_inbox) && (
          <button
            type="button"
            className={`mobile-nav-item ${view === 'approvals' && !moreSheetOpen ? 'active' : ''}`}
            onClick={() => { setView('approvals'); setMoreSheetOpen(false); }}
          >
            <span className="mobile-nav-icon" style={{ position: 'relative' }}>
              ✓
              {approvalInboxCount > 0 && <span className="mobile-badge-pill">{approvalInboxCount}</span>}
            </span>
            <span>Approvals</span>
          </button>
        )}

        {(Boolean(system?.features?.skills) || view === 'skills') && (
          <button
            type="button"
            className={`mobile-nav-item ${view === 'skills' && !moreSheetOpen ? 'active' : ''}`}
            onClick={() => { setView('skills'); setMoreSheetOpen(false); }}
          >
            <span className="mobile-nav-icon">🛠</span>
            <span>Skills</span>
          </button>
        )}

        <button
          type="button"
          className={`mobile-nav-item ${moreSheetOpen ? 'active' : ''}`}
          ref={moreButtonRef}
          onClick={() => setMoreSheetOpen(prev => !prev)}
        >
          <span className="mobile-nav-icon">⋯</span>
          <span>More</span>
        </button>
      </nav>
    )}
    {settingsOpen && <ProviderSettings config={providerConfig} busy={busy} onClose={() => setSettingsOpen(false)} onSave={async payload => {
      setBusy(true); setError('')
      try {
        const result = await api<{ config: ProviderConfig }>('/api/config/provider', { method: 'POST', body: JSON.stringify(payload) })
        setProviderConfig(result.config)
      } catch (reason) { setError(reason instanceof Error ? reason.message : 'Could not save settings'); throw reason }
      finally { setBusy(false) }
    }} />}
  </main>
}

function ProviderSettings({ config, busy, onClose, onSave }: {
  config: ProviderConfig | null; busy: boolean; onClose: () => void
  onSave: (payload: Record<string, string>) => Promise<void>
}) {
  const [draft, setDraft] = useState({ provider_id: '', provider_name: '', model: '', reasoning_effort: 'minimal', base_url: '', wire_api: 'responses', env_key: 'OPENAI_API_KEY', api_key: '' })
  const [saved, setSaved] = useState(false)
  useEffect(() => { if (config) setDraft({ ...config, api_key: '' }) }, [config])
  function field(name: keyof typeof draft, value: string) { setSaved(false); setDraft(previous => ({ ...previous, [name]: value })) }
  return <div className="settings-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) onClose() }}>
    <section className="settings-sheet" role="dialog" aria-modal="true" aria-label="Provider settings">
      <header><div><p className="eyebrow">MODEL PROVIDER</p><h2>Configuration</h2><p>OpenAI-compatible provider settings used by new Conveyor tasks.</p></div><button className="close-button" onClick={onClose} aria-label="Close settings">×</button></header>
      {!config ? <div className="settings-loading">Loading configuration…</div> : <form onSubmit={event => { event.preventDefault(); void onSave(draft).then(() => { setSaved(true); setDraft(previous => ({ ...previous, api_key: '' })) }).catch(() => {}) }}>
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

function StatusBadge({ state }: { state: string }) { return <span className={`status-badge ${state}`}><i />{stateLabel(state)}</span> }
function Empty({ text }: { text: string }) { return <div className="empty">{text}</div> }
function KeyValue({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) { return <div className="key-value"><span>{label}</span><strong className={mono ? 'mono' : ''}>{value}</strong></div> }
function ContextSection({
  title,
  children,
  collapsible = false,
  storageKey,
  defaultCollapsed = false,
  className = '',
}: {
  title: string
  className?: string
  children: React.ReactNode
  collapsible?: boolean
  storageKey?: string
  defaultCollapsed?: boolean
}) {
  const [collapsed, setCollapsed] = useState(() => {
    if (!collapsible || !storageKey) return defaultCollapsed
    try {
      const stored = localStorage.getItem(storageKey)
      return stored !== null ? stored === 'true' : defaultCollapsed
    } catch {
      return defaultCollapsed
    }
  })

  const toggle = () => {
    setCollapsed(prev => {
      const next = !prev
      if (storageKey) {
        try {
          localStorage.setItem(storageKey, String(next))
        } catch {}
      }
      return next
    })
  }

  return (
    <section className={`context-section ${className} ${collapsible ? 'is-collapsible' : ''} ${collapsed ? 'is-collapsed' : ''}`}>
      <h3 className="eyebrow context-section-header">
        {collapsible ? (
          <button
            type="button"
            className="context-section-toggle"
            aria-expanded={!collapsed}
            onClick={toggle}
          >
            <span>{title.toUpperCase()}</span>
            <span className="context-collapse-icon" aria-hidden="true">
              {collapsed ? '▸' : '▾'}
            </span>
          </button>
        ) : (
          title.toUpperCase()
        )}
      </h3>
      {!collapsed && children}
    </section>
  )
}
function AuthenticatedImage({ artifact, token }: { artifact: ComputerStatus['screenshots'][number]; token: string }) {
  const [url, setUrl] = useState('')
  const [expanded, setExpanded] = useState(false)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)
  const wasExpandedRef = useRef(false)
  useEffect(() => {
    let active = true; let localUrl = ''
    setUrl('')
    void fetch(`/api/artifacts/${encodeURIComponent(artifact.artifact_id)}`, { headers: { Authorization: `Bearer ${token}` } })
      .then(response => response.ok ? response.blob() : Promise.reject())
      .then(blob => { if (active) { localUrl = URL.createObjectURL(blob); setUrl(localUrl) } })
      .catch(() => {})
    return () => { active = false; if (localUrl) URL.revokeObjectURL(localUrl) }
  }, [artifact.artifact_id, token])
  useEffect(() => {
    if (!expanded) {
      if (wasExpandedRef.current) triggerRef.current?.focus()
      wasExpandedRef.current = false
      return
    }
    wasExpandedRef.current = true
    closeRef.current?.focus()
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === 'Escape') setExpanded(false) }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [expanded])
  return url ? <>
    <figure className="screenshot host-screen-preview">
      <button ref={triggerRef} type="button" className="screen-image-trigger" onClick={() => setExpanded(true)} aria-label="Open latest host screen preview">
        <img src={url} alt="Latest captured host computer screen" />
        <span>Open screen</span>
      </button>
      <figcaption><span>{artifact.width && artifact.height ? `${artifact.width} × ${artifact.height} · ` : ''}{formatTime(artifact.created_at)}</span><span>{artifact.node_id || 'Mac node'}</span></figcaption>
    </figure>
    {expanded && <div className="screen-viewer-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) setExpanded(false) }}>
      <section className="screen-viewer" role="dialog" aria-modal="true" aria-labelledby="screen-viewer-title" onKeyDown={event => {
        if (event.key !== 'Tab') return
        const focusable = event.currentTarget.querySelectorAll<HTMLElement>('button:not([disabled]), [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')
        if (focusable.length === 0) { event.preventDefault(); return }
        const first = focusable[0]; const last = focusable[focusable.length - 1]
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
      }}>
        <header className="screen-viewer-header">
          <div><p className="eyebrow">MAC NODE · READ-ONLY</p><h2 id="screen-viewer-title">Latest host screen</h2><p>{artifact.width && artifact.height ? `${artifact.width} × ${artifact.height} · ` : ''}Captured {formatTime(artifact.created_at)}</p></div>
          <button ref={closeRef} type="button" className="close-button" onClick={() => setExpanded(false)} aria-label="Close screen preview">×</button>
        </header>
        <div className="screen-viewer-image-frame"><img src={url} alt="Expanded latest captured host computer screen" /></div>
        <footer className="screen-viewer-footer"><span>{artifact.node_id || 'Mac node'} · preview thumbnail</span><span>The original screenshot remains on the Mac. No continuous stream or remote input is enabled.</span></footer>
      </section>
    </div>}
  </> : null
}
function EventCard({ item }: { item: EventItem }) {
  const isTool = item.kind.startsWith('tool.'); const text = String(item.payload.text || item.payload.output || item.payload.result || item.payload.error || '')
  if (isTool) return <details className={`event-card tool-event ${item.kind.endsWith('failed') ? 'failed' : ''}`} open={item.kind.endsWith('failed')}>
    <summary><span className="tool-icon">⌘</span><span><strong>{String(item.payload.name || 'Tool')}</strong><small>{item.kind.replace('tool.', '')}</small></span><time>{formatTime(item.timestamp)}</time><b>⌄</b></summary>
    {text && <pre>{text.slice(0, 12000)}</pre>}
  </details>
  if (item.kind.startsWith('approval.')) return <article className="event-card approval-event"><div className="event-meta"><span>APPROVAL</span><time>{formatTime(item.timestamp)}</time></div><p>{item.kind.replace('.', ' ')} · {String(item.payload.action || '')}</p></article>
  return <article className={`event-card ${item.kind.startsWith('assistant.') ? 'assistant-event' : 'system-event'}`}><div className="event-meta"><span>{item.kind.startsWith('assistant.') ? 'CONVEYOR' : item.kind.toUpperCase()}</span><time>{formatTime(item.timestamp)}</time></div><p>{text || item.kind.replace('.', ' ')}</p></article>
}
