import { FormEvent, Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ChatPanel, type ChatPanelProps } from './components/ChatPanel'
const SupervisorWorkspace = lazy(() => import('./components/SupervisorWorkspace'))
import { AgentWorkspace } from './components/AgentWorkspace'
import { FormattedText } from './components/FormattedText'
import { InboxPanel } from './components/InboxPanel'
import { MemoryPanel } from './components/MemoryPanel'
import { SkillsPanel } from './components/SkillsPanel'
import { WorkflowPanel } from './components/WorkflowPanel'
import { ConnectorsPanel } from './components/ConnectorsPanel'
import { SecretsPanel } from './components/SecretsPanel'
import { WorkersPanel } from './components/WorkersPanel'
import { ApprovalInboxPanel } from './components/ApprovalInboxPanel'
import { PwaNotifications } from './components/PwaNotifications'
import { leavePwa } from './pwa'
import { BrowserApprovalsPanel } from './components/BrowserApprovalsPanel'
import { TeammatePanel } from './components/TeammatePanel'
import { LiveScreenPanel } from './components/LiveScreenPanel'
import { AgentLibrary } from './components/AgentLibrary'
import { AgentAvatar, AgentDialog, AgentList, agentTag, relativeTime, type Agent, type AgentDraft } from './components/AgentList'
import { RuntimeOwnerCard } from './components/RuntimeOwnerCard'
import { TranscriptPanel } from './components/TranscriptPanel'
import { runtimeOwnerFromJob, terminalJobState, type TranscriptMessage } from './runtime'
import { dropApproval, isStale, shouldRefreshForEvent } from './approvalFreshness'

import { mergeJobEvents, streamedText, type JobEvent as EventItem } from './jobEvents'
import { CONNECTION_LABELS, LockConfirmDialog, SHORTCUT_PREFIX, ShellIcon, VIEW_LABELS, VIEW_SUBTITLES, findVisibleComposer, isTypingTarget, shellErrorText, urlForView, useMediaQuery, viewFromLocation, type ConnectionState, type ShellView } from './ux/shellNav'
import { ChangedFileList, ClampedText, DiffBody, DiffStat, ToolCallCard } from './components/TaskParts'
import { SetupChecklist } from './components/SetupChecklist'
import { changesDiffFrom, changesOutcome, describeJobError, durationText, emptyChangesDiff, eventKindLabel, expiryText, groupToolCalls, jobErrorSummary, modeLabel, nodeStatusLabel, nodeTypeLabel, toolDisplayName, type ChangesDiff, type ChangesOutcome, type DiffResponse } from './taskText'
type Job = {
  id: string; state: string; mode: string; channel: string; chat_id: string; operator_id?: string
  created_at: string; updated_at?: string; started_at?: string; finished_at?: string
  prompt_preview: string; metadata?: Record<string, unknown>; latest_event?: EventItem
  error?: string
  changed_files?: { status: string; path: string }[]
  runtime?: Record<string, unknown>
}
type ActiveRefinement = {
  chain_id: string; state: string; turn_count: number
  root_queue_job_id?: string; latest_queue_job_id?: string
  latest_runtime_job_id?: string; updated_at?: string
}
type RunSummary = {
  id: string; state: string; mode: string; created_at?: string; updated_at?: string
  finished_at?: string; prompt_preview?: string; refinement_intent: boolean; refinement_turn?: number
}
type Session = {
  id: string; channel?: string; title?: string; created_at: string; updated_at?: string; last_activity: string
  operator_id?: string; source_chat_id?: string; job_count: number; message_count?: number; latest_job?: Job
  active_refinement?: ActiveRefinement | null
  /** kind of the newest stored message: 'error' means the last turn failed (QA N30). */
  last_message_kind?: string | null
}
type SessionDetail = Session & { messages?: TranscriptMessage[]; jobs?: Job[]; runs?: RunSummary[] }
// Chat-only sessions stay in Chat; promoted conversations with jobs also appear in Tasks.
const isChatSessionId = (id: string) => id.includes(':webchat-') || id.startsWith('webchat-')
const isChatSession = (session: Session) => isChatSessionId(session.id) || (session.source_chat_id || '').startsWith('webchat-')
// 项目 (Supervisor) worktree conversations: read-only in chat and belong to the 项目 page.
// They never join the 任务 / 对话 lists, so the owner is never dropped into one that
// answers 409 (N17 / N21).
const isSupervisorWorktreeSessionId = (id: string) => id.includes('supwt-')
const isSupervisorWorktreeSession = (session: Session) => isSupervisorWorktreeSessionId(session.id)
const taskSessionsOnly = (items: Session[]) => items.filter(item =>
  !isSupervisorWorktreeSession(item) && (!isChatSession(item) || item.job_count > 0 || Boolean(item.latest_job)))
// 对话 lists every web conversation that already has messages, including the chat-only
// sessions the 任务 list leaves out. A session with no messages is not worth reopening,
// so the empty state only shows when nothing was ever said.
const chatSessionsOnly = (items: Session[]) => items.filter(item =>
  !isSupervisorWorktreeSession(item)
  && (item.channel || 'web') === 'web' && ((item.message_count || 0) > 0 || (item.job_count || 0) > 0))
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
    workflows?: boolean
    provider_key_scoping?: boolean
    mobile_ui?: boolean
    pwa?: boolean
    mcp?: boolean
    teammate?: boolean
    agents?: boolean
    workers?: boolean
    agent_supervisor?: boolean
    /** The server already has a usable chat model (saved in 模型设置 or from .env). */
    chat_model_configured?: boolean
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
  /** Any usable chat model on the server, including one configured by env vars. */
  chat_model_configured?: boolean
}

const statusOrder = ['running', 'queued', 'interrupted', 'failed', 'cancelled', 'completed']

// zh-CN times: 刚刚 / 5 分钟前 / 今天 14:05 / 昨天 14:05 / 11月3日 14:05 / 2025年11月3日 14:05
function formatTime(value?: string) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.valueOf())) return value
  const now = new Date()
  const ago = now.getTime() - date.getTime()
  if (ago >= -60_000 && ago < 60_000) return '刚刚'
  if (ago > 0 && ago < 3_600_000) return `${Math.floor(ago / 60_000)} 分钟前`
  const clock = `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
  const day = (offset: number) => { const d = new Date(now); d.setDate(d.getDate() - offset); return d.toDateString() }
  if (date.toDateString() === day(0)) return `今天 ${clock}`
  if (date.toDateString() === day(1)) return `昨天 ${clock}`
  const monthDay = `${date.getMonth() + 1}月${date.getDate()}日`
  return date.getFullYear() === now.getFullYear() ? `${monthDay} ${clock}` : `${date.getFullYear()}年${monthDay} ${clock}`
}
function bytes(value: number | null) {
  if (value == null) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']; let n = value; let i = 0
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++ }
  return `${n.toFixed(i > 1 ? 1 : 0)} ${units[i]}`
}
const STATE_LABELS: Record<string, string> = {
  queued: '排队中', running: '运行中', completed: '已完成', failed: '失败', cancelled: '已取消',
  interrupted: '已中断', idle: '空闲', pending: '等待中', claimed: '处理中', expired: '已过期',
  unknown: '未知', waiting_for_human: '等待人工', human_active: '人工处理中', approved: '已批准',
  rejected: '已拒绝', accepted: '已接受', applied: '已应用', discarded: '已放弃', paused: '已暂停',
}
function stateLabel(state?: string) {
  const key = state || 'unknown'
  return STATE_LABELS[key] || key.replaceAll('_', ' ')
}
function sessionLabel(session: Session | undefined) {
  return session?.title || session?.latest_job?.prompt_preview || '新会话'
}
// A session whose last turn failed is marked in the list, so it never reads like a finished one (N30).
function sessionListTitle(session: Session) {
  const base = session.title || '未命名会话'
  return session.last_message_kind === 'error' ? `${base}（没有完成）` : base
}
function storedToken() {
  try { return localStorage.getItem('conveyor-token') || '' } catch { return '' }
}
function forgetToken() {
  try { localStorage.removeItem('conveyor-token') } catch { /* storage unavailable */ }
}
// Why a host screenshot failed, for the codes the Mac helper reports most often.
const SCREEN_REQUEST_ERRORS: Record<string, string> = {
  screen_recording_permission_required: 'Mac 还没有允许屏幕录制',
  desktop_agent_offline: 'Mac 不在线',
  desktop_node_disabled: '电脑节点没有开启',
  screenshot_helper_timeout: '截图超时',
  human_takeover_active: '人工接管期间不能截屏',
}
function hostScreenRequestLabel(request: ComputerStatus['screen_request']) {
  if (!request) return ''
  if (request.status === 'pending') return '等待 Mac 响应…'
  if (request.status === 'claimed') return '正在 Mac 上截屏…'
  if (request.status === 'completed' && ['pending', 'claimed'].includes(request.upload_status || '')) return '正在生成缩略图…'
  if (request.status === 'completed' && request.upload_status === 'completed') return '最新截图已就绪'
  if (request.status === 'completed' && request.upload_status === 'failed') return '已截屏，但缩略图没有传上来'
  if (request.status === 'completed') return '已在 Mac 上截屏，预览无法上传'
  if (request.status === 'failed') {
    const reason = request.error ? SCREEN_REQUEST_ERRORS[request.error] : ''
    return '截屏失败' + (reason ? `：${reason}` : '')
  }
  if (request.status === 'expired') return '截屏请求已过期'
  if (request.status === 'cancelled') return '截屏请求已取消'
  return '截屏 · ' + stateLabel(request.status)
}
/** An unknown failure code is shown only as a muted technical detail under the sentence. */
function hostScreenRequestDetail(request: ComputerStatus['screen_request']) {
  if (request?.status !== 'failed' || !request.error || SCREEN_REQUEST_ERRORS[request.error]) return ''
  return request.error
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
  const [token, setToken] = useState(() => sessionStorage.getItem('conveyor-token') || storedToken())
  const [rememberToken, setRememberToken] = useState(() => Boolean(storedToken()))
  const [agentState, setAgentState] = useState<{ enabled: boolean; agents: Agent[] }>({ enabled: false, agents: [] })
  const [selectedAgentId, setSelectedAgentId] = useState(() => localStorage.getItem('conveyor-agent') || '')
  const [agentDialog, setAgentDialog] = useState<null | { agent?: Agent }>(null)
  const [agentTab, setAgentTab] = useState<'details' | 'library' | 'computer'>('details')
  // With agents on, the selected agent's one conversation is the session.
  const selectedAgent = agentState.enabled ? (agentState.agents.find(agent => agent.id === selectedAgentId) || agentState.agents[0]) : undefined
  const agentSessionRef = useRef('')
  agentSessionRef.current = selectedAgent?.session_id || ''
  // Agents are conversations first: land on Chat the moment they are on, unless the URL named a view.
  useEffect(() => {
    if (!agentState.enabled || explicitRouteRef.current || viewRef.current !== 'tasks') return
    historyModeRef.current = 'replace'
    setView('chat')
  }, [agentState.enabled])
  const [tokenDraft, setTokenDraft] = useState('')
  const [authenticated, setAuthenticated] = useState(false)
  // 刷新时带着记住的令牌（或这个标签页里已经存过的令牌）先别亮出解锁表单：先显示一个
  // 没有输入框的「正在打开…」，等服务器确认令牌有效再进应用。否则表单会先闪一秒，
  // 老板这期间打的字全都落到光标底下突然出现的输入框里。
  const [checkingStoredToken, setCheckingStoredToken] = useState(() => Boolean(sessionStorage.getItem('conveyor-token') || storedToken()))
  const [sessions, setSessions] = useState<Session[]>([])
  // 对话 sidebar rows: web conversations that already have messages (chat-only ones included).
  const [chatSessions, setChatSessions] = useState<Session[]>([])
  const [anySessionHasMessages, setAnySessionHasMessages] = useState(false)
  const [jobs, setJobs] = useState<Job[]>([])
  const [selectedSessionId, setSelectedSessionId] = useState(() => sessionStorage.getItem('conveyor-selected-session') || '')
  const [selectedJobId, setSelectedJobId] = useState('')
  const [creatingSession, setCreatingSession] = useState(false)
  const [transcript, setTranscript] = useState<TranscriptMessage[]>([])
  const [selectedSessionDetail, setSelectedSessionDetail] = useState<SessionDetail | null>(null)
  const [sessionDetailError, setSessionDetailError] = useState('')
  const [transcriptError, setTranscriptError] = useState(false)
  const [events, setEvents] = useState<EventItem[]>([])
  const [approvals, setApprovals] = useState<Approval[]>([])
  const [nodes, setNodes] = useState<NodeInfo[]>([])
  const [system, setSystem] = useState<SystemStatus | null>(null)
  const [computer, setComputer] = useState<ComputerStatus | null>(null)
  const [changesDiff, setChangesDiff] = useState<ChangesDiff>(emptyChangesDiff)
  const [changesResult, setChangesResult] = useState<{ jobId: string; outcome: ChangesOutcome } | null>(null)
  const [prompt, setPrompt] = useState('')
  const [mode, setMode] = useState<'run' | 'fix'>('run')
  const [executionOnly, setExecutionOnly] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  // POST /api/tasks failed: the reason sits next to the composer, never only as a border.
  const [submitError, setSubmitError] = useState<{ text: string; detail?: string } | null>(null)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [screenBusy, setScreenBusy] = useState(false)
  const [screenError, setScreenError] = useState<{ text: string; detail?: string } | null>(null)
  const [providerConfig, setProviderConfig] = useState<ProviderConfig | null>(null)
  const [workersData, setWorkersData] = useState<any>(null)
  const [checklistDismissed, setChecklistDismissed] = useState(() => {
    try { return localStorage.getItem('conveyor-onboarding-dismissed') === 'true' } catch { return false }
  })
  // The view lives in the URL hash (#chat, #memory, …; legacy /memory paths still open it).
  const [view, setView] = useState<ShellView>(() => viewFromLocation() || 'tasks')
  const viewRef = useRef(view)
  viewRef.current = view
  const explicitRouteRef = useRef(Boolean(viewFromLocation()))
  // Every view change adds a history entry, except redirects and the first sync (replace).
  const historyModeRef = useRef<'push' | 'replace'>('replace')
  const [inboxUnread, setInboxUnread] = useState(0)
  const [approvalInboxCount, setApprovalInboxCount] = useState(0)
  // 项目 badge and title prefix: goals (目标) waiting for the owner's reply.
  const [goalsWaiting, setGoalsWaiting] = useState(0)
  const [chatDraft, setChatDraft] = useState('')
  // 对话: which conversation the panel shows, and the one a sidebar row asked it to open.
  const [chatSessionId, setChatSessionId] = useState('')
  const [chatOpenRequest, setChatOpenRequest] = useState<{ id: string; nonce: number } | null>(null)
  // A message sent from 对话 right now: checklist step ③ ticks before the next poll.
  const [chatMessageSent, setChatMessageSent] = useState(false)
  const [realtimeState, setRealtimeState] = useState<'connecting' | 'live' | 'reconnecting'>('connecting')
  const sessionDetailFetchGen = useRef(0)
  const transcriptRequestRef = useRef<{ sessionId: string; generation: number } | null>(null)
  const transcriptSessionRef = useRef(selectedSessionId)
  transcriptSessionRef.current = selectedSessionId
  const lastSequence = useRef(0)
  const refreshGen = useRef(0)
  const approvalInboxFetchGen = useRef(0)
  const streamRef = useRef<HTMLDivElement>(null)

  const [moreSheetOpen, setMoreSheetOpen] = useState(false)
  const [sessionsDrawerOpen, setSessionsDrawerOpen] = useState(false)
  const [contextDrawerOpen, setContextDrawerOpen] = useState(false)
  // Shell layout: ≤820px always gets the phone layout (drawers + bottom nav), whatever
  // features.mobile_ui says. The right panel is a desktop toggle, collapsed by default <1200px.
  const narrowViewport = useMediaQuery('(max-width: 820px)')
  const wideViewport = useMediaQuery('(min-width: 1200px)')
  const [contextPref, setContextPref] = useState<'open' | 'closed' | null>(() => {
    try { const stored = localStorage.getItem('conveyor-context-panel'); return stored === 'open' || stored === 'closed' ? stored : null } catch { return null }
  })
  const [navMenuOpen, setNavMenuOpen] = useState(false)
  const [lockConfirmOpen, setLockConfirmOpen] = useState(false)
  const [syncStale, setSyncStale] = useState(false)
  const [isOnline, setIsOnline] = useState(() => typeof navigator !== 'undefined' ? navigator.onLine : true)
  const [initialLoaded, setInitialLoaded] = useState(false)
  const [pollFailed, setPollFailed] = useState(false)
  const [notice, setNotice] = useState('')
  const pollAbortRef = useRef<AbortController>(new AbortController())
  const isLockedRef = useRef(false)
  const pollTimerRef = useRef<number | null>(null)
  const approvalTimerRef = useRef<number | null>(null)
  const inboxTimerRef = useRef<number | null>(null)
  const goalsTimerRef = useRef<number | null>(null)
  const staleTimerRef = useRef<number | null>(null)
  const navMoreRef = useRef<HTMLDivElement>(null)
  const moreSheetRef = useRef<HTMLDivElement>(null)
  const drawerOpenerRef = useRef<HTMLElement | null>(null)

  useEffect(() => {
    // Preserve mobile UI's legacy manifest even with CONVEYOR_PWA disabled.
    // PWA adds installability without gating the existing mobile manifest.
    if (system?.features?.mobile_ui || (authenticated && system?.features?.pwa)) {
      let link = document.querySelector<HTMLLinkElement>('link[rel="manifest"]')
      if (!link) {
        link = document.createElement('link')
        link.rel = 'manifest'
        link.href = '/manifest.webmanifest'
        document.head.appendChild(link)
      }
    } else {
      document.querySelector<HTMLLinkElement>('link[rel="manifest"]')?.remove()
    }
  }, [authenticated, system?.features?.pwa, system?.features?.mobile_ui])

  const sessionsDrawerRef = useRef<HTMLElement>(null)
  const contextDrawerRef = useRef<HTMLElement>(null)
  const moreButtonRef = useRef<HTMLButtonElement>(null)
  const anyMobileOverlayOpen = moreSheetOpen || sessionsDrawerOpen || contextDrawerOpen

  // Latest overlay state for the always-on listeners below (read through a ref so the
  // listeners never depend on React re-registering them at the right moment).
  const overlayStateRef = useRef({ more: false, sessions: false, context: false })
  overlayStateRef.current = { more: moreSheetOpen, sessions: sessionsDrawerOpen, context: contextDrawerOpen }
  const mobileUiOn = Boolean(system?.features?.mobile_ui) || narrowViewport

  // Primary navigation, ordered by use; the rarely used pages form a second group
  // ("更多" when space is short). Alt+1…9 follow this order.
  const features = system?.features
  const navItems = useMemo(() => {
    const items: { view: ShellView; group: 'main' | 'more'; badge?: number; badgeLabel?: string }[] = [
      { view: 'chat', group: 'main' },
      { view: 'tasks', group: 'main' },
    ]
    if (features?.agent_supervisor) {
      items.push({ view: 'code', group: 'main', badge: goalsWaiting, badgeLabel: goalsWaiting > 0 ? `有 ${goalsWaiting} 个目标在等你回复` : undefined })
    }
    items.push({ view: 'inbox', group: 'main', badge: inboxUnread })
    if (features?.approval_inbox) items.push({ view: 'approvals', group: 'main', badge: approvalInboxCount })
    if (features?.long_term_memory || view === 'memory') items.push({ view: 'memory', group: 'main' })
    if ((features?.teammate ?? true) || view === 'teammate') items.push({ view: 'teammate', group: 'main' })
    items.push({ view: 'browser', group: 'more' }, { view: 'secrets', group: 'more' })
    if (features?.workers) items.push({ view: 'workers', group: 'more' })
    if (features?.skills || view === 'skills') items.push({ view: 'skills', group: 'more' })
    if (features?.workflows || view === 'workflows') items.push({ view: 'workflows', group: 'more' })
    if (features?.mcp || view === 'connectors') items.push({ view: 'connectors', group: 'more' })
    return items
  }, [features?.agent_supervisor, features?.approval_inbox, features?.long_term_memory, features?.teammate, features?.skills, features?.workflows, features?.mcp, features?.workers, view, inboxUnread, approvalInboxCount, goalsWaiting])
  const shortcutViewsRef = useRef<ShellView[]>([])
  shortcutViewsRef.current = navItems.slice(0, 9).map(item => item.view)

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
  // when it closes, hand focus back to the button that opened it (or the More button)
  // instead of the hidden drawer (which may already have dropped focus to <body>).
  const drawerWasOpenRef = useRef(false)
  useEffect(() => {
    const drawer = sessionsDrawerOpen ? sessionsDrawerRef.current : contextDrawerOpen ? contextDrawerRef.current : null
    if (drawer) {
      drawerWasOpenRef.current = true
      drawer.focus({ preventScroll: true })
      return
    }
    if (!drawerWasOpenRef.current) return
    drawerWasOpenRef.current = false
    const active = document.activeElement
    if (!active || active === document.body || sessionsDrawerRef.current?.contains(active) || contextDrawerRef.current?.contains(active)) {
      const opener = drawerOpenerRef.current
      ;(opener?.isConnected ? opener : moreButtonRef.current)?.focus({ preventScroll: true })
    }
  }, [sessionsDrawerOpen, contextDrawerOpen])

  // The More sheet takes focus when it opens and gives it back to the More button.
  const moreSheetWasOpenRef = useRef(false)
  useEffect(() => {
    if (moreSheetOpen) {
      moreSheetWasOpenRef.current = true
      moreSheetRef.current?.focus({ preventScroll: true })
      return
    }
    if (!moreSheetWasOpenRef.current) return
    moreSheetWasOpenRef.current = false
    if (!document.activeElement || document.activeElement === document.body) moreButtonRef.current?.focus({ preventScroll: true })
  }, [moreSheetOpen])

  // URL ⇄ view: a view change writes #view (a new history entry; redirects replace it),
  // back/forward and edited hashes switch the view. The query string (?agent=, ?approval=) is kept.
  useEffect(() => {
    if (!authenticated) return
    const target = urlForView(view)
    if (`${window.location.pathname}${window.location.search}${window.location.hash}` !== target) {
      if (historyModeRef.current === 'push') window.history.pushState(window.history.state, '', target)
      else window.history.replaceState(window.history.state, '', target)
    }
    historyModeRef.current = 'push'
  }, [authenticated, view])
  // Tab title: the page name, prefixed with 「(N) 」 while N goals wait for the owner's reply.
  useEffect(() => {
    if (!authenticated) { document.title = 'Conveyor 控制台'; return }
    document.title = `${goalsWaiting > 0 ? `(${goalsWaiting}) ` : ''}${VIEW_LABELS[view]} · Conveyor 控制台`
  }, [authenticated, view, goalsWaiting])
  useEffect(() => {
    const followLocation = () => {
      const next = viewFromLocation()
      if (!next || next === viewRef.current) return
      historyModeRef.current = 'replace'
      setView(next)
    }
    window.addEventListener('popstate', followLocation)
    window.addEventListener('hashchange', followLocation)
    return () => {
      window.removeEventListener('popstate', followLocation)
      window.removeEventListener('hashchange', followLocation)
    }
  }, [])
  // A new view starts with menus, sheets and drawers closed.
  useEffect(() => {
    setNavMenuOpen(false)
    setMoreSheetOpen(false)
    setSessionsDrawerOpen(false)
    setContextDrawerOpen(false)
  }, [view])
  // Drawers and the 更多 sheet exist only on phones: widening past 820px (rotating a tablet,
  // resizing a window) closes them, so a stale aria-modal cannot block the keyboard shortcuts.
  useEffect(() => {
    if (narrowViewport) return
    setMoreSheetOpen(false)
    setSessionsDrawerOpen(false)
    setContextDrawerOpen(false)
  }, [narrowViewport])

  // Keyboard: Alt+1…9 switch views, "/" focuses the message box of the view on screen.
  useEffect(() => {
    if (!authenticated) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.isComposing || event.keyCode === 229) return
      const modalOpen = Boolean(document.querySelector('[aria-modal="true"]'))
      if (event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey && /^Digit[1-9]$/.test(event.code)) {
        const target = shortcutViewsRef.current[Number(event.code.slice(5)) - 1]
        if (!target || modalOpen) return
        event.preventDefault()
        setView(target)
        return
      }
      if (event.key === '/' && !event.altKey && !event.ctrlKey && !event.metaKey) {
        if (modalOpen || isTypingTarget(event.target)) return
        const box = findVisibleComposer()
        if (!box) return
        event.preventDefault()
        box.focus()
        box.setSelectionRange(box.value.length, box.value.length)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [authenticated])

  // The desktop "更多" menu closes on Esc and on any click outside it.
  useEffect(() => {
    if (!navMenuOpen) return
    const onPointerDown = (event: PointerEvent) => {
      if (!navMoreRef.current?.contains(event.target as Node)) setNavMenuOpen(false)
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (!isEscapeKey(event)) return
      event.preventDefault()
      setNavMenuOpen(false)
      navMoreRef.current?.querySelector<HTMLButtonElement>('button')?.focus()
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [navMenuOpen])

  // The 收件箱 badge stays current without opening the page (the open page reports its own count).
  const inboxOpen = view === 'inbox'
  useEffect(() => {
    if (!authenticated || !system?.features?.routines || inboxOpen) return
    let stopped = false
    const load = () => {
      if (document.visibilityState !== 'visible' || !token || isLockedRef.current) return
      void fetch('/api/inbox?limit=1', { headers: { Authorization: `Bearer ${token}` }, signal: pollAbortRef.current.signal })
        .then(response => response.ok ? response.json() : null)
        .then(data => { if (!stopped && data && Number.isFinite(Number(data.unread))) setInboxUnread(Number(data.unread)) })
        .catch(() => {})
    }
    load()
    const timer = window.setInterval(load, 30_000)
    inboxTimerRef.current = timer
    return () => { stopped = true; window.clearInterval(timer); inboxTimerRef.current = null }
  }, [authenticated, system?.features?.routines, inboxOpen, token])

  // The 项目 badge: goals waiting for the owner's reply, read every 15 s while the console is
  // unlocked and the 项目 page exists (features.agent_supervisor). A hidden tab stops the poll;
  // showing it again reads at once and restarts it. Leaving 项目 also reads at once.
  const supervisorOn = Boolean(system?.features?.agent_supervisor)
  const codeOpen = view === 'code'
  useEffect(() => {
    if (!authenticated || !supervisorOn || !token) { setGoalsWaiting(0); return }
    let stopped = false
    let generation = 0
    let timer: number | null = null
    const load = () => {
      if (stopped || document.visibilityState !== 'visible' || isLockedRef.current) return
      const gen = ++generation
      void fetch('/api/supervisor/goals', { headers: { Authorization: `Bearer ${token}` }, signal: pollAbortRef.current.signal })
        .then(response => response.ok ? response.json() : null)
        .then(data => {
          if (stopped || gen !== generation || !data || !Array.isArray(data.goals)) return
          setGoalsWaiting(data.goals.filter((goal: { state?: string }) => goal?.state === 'waiting_user').length)
        })
        .catch(() => {})
    }
    const stop = () => {
      if (timer !== null) window.clearInterval(timer)
      timer = null
      goalsTimerRef.current = null
    }
    const start = () => {
      stop()
      if (document.visibilityState !== 'visible') return
      load()
      timer = window.setInterval(load, 15_000)
      goalsTimerRef.current = timer
    }
    const onVisibility = () => { if (document.visibilityState === 'visible') start(); else stop() }
    start()
    document.addEventListener('visibilitychange', onVisibility)
    return () => { stopped = true; stop(); document.removeEventListener('visibilitychange', onVisibility) }
  }, [authenticated, supervisorOn, token, codeOpen])

  // Honest connection state: every successful status poll proves the server is reachable;
  // no poll for 12s means the console is reconnecting.
  useEffect(() => {
    if (!system) return
    setSyncStale(false)
    const timer = window.setTimeout(() => setSyncStale(true), 12_000)
    staleTimerRef.current = timer
    return () => { window.clearTimeout(timer); staleTimerRef.current = null }
  }, [system])

  const selectSession = useCallback((sessionId: string, jobId?: string) => {
    setSelectedSessionDetail(null)
    setTranscript([])
    sessionDetailFetchGen.current++
    transcriptSessionRef.current = sessionId
    setTranscriptError(false)
    setExecutionOnly(false)
    setCreatingSession(false)
    setSelectedSessionId(sessionId)
    if (sessionId) sessionStorage.setItem('conveyor-selected-session', sessionId)
    else sessionStorage.removeItem('conveyor-selected-session')
    setSelectedJobId(jobId || '')
    setTranscript([])
  }, [])

  // The 会话 sidebar serves both surfaces: 任务 keeps its task runs (chat-only
  // conversations live in 对话), 对话 lists every web conversation with messages.
  const sidebarSessions = view === 'chat' ? chatSessions : sessions
  const sidebarActiveId = view === 'chat' ? chatSessionId : selectedSessionId
  const openConversation = useCallback((session: Session) => {
    if (view === 'chat' && !agentState.enabled) {
      // 对话 reopens the conversation in the chat panel itself, with its messages.
      // A nonce makes a repeated click on the same row a fresh request.
      setChatOpenRequest(previous => ({ id: session.id, nonce: (previous?.nonce ?? 0) + 1 }))
      return
    }
    selectSession(session.id, session.latest_job?.id)
  }, [agentState.enabled, selectSession, view])

  const api = useCallback(async <T,>(path: string, init?: RequestInit): Promise<T> => {
    if (!token || isLockedRef.current) throw new DOMException('Aborted', 'AbortError')
    const signal = init?.signal || pollAbortRef.current.signal
    const response = await fetch(path, {
      ...init,
      signal,
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}`, ...(init?.headers || {}) },
    })
    if (response.status === 401) { forgetToken(); if (system?.features?.pwa) void leavePwa(token).catch(() => undefined); setAuthenticated(false); throw new Error('Token rejected') }
    const body = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(body.error || body.message || `Request failed (${response.status})`)
    return body as T
  }, [token, system?.features?.pwa])

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
        setChatSessions(chatSessionsOnly(data.sessions))
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
    if (!token || isLockedRef.current) return
    const gen = ++refreshGen.current
    try {
      const [sessionData, jobData, approvalData, nodeData, systemData, computerData, agentData, providerData, workersPayload] = await Promise.all([
        api<{ sessions: Session[] }>('/api/sessions'), api<{ jobs: Job[] }>('/api/jobs'),
        api<{ approvals: Approval[] }>('/api/approvals'), api<{ nodes: NodeInfo[] }>('/api/nodes'),
        api<SystemStatus>('/api/system/status'), api<ComputerStatus>('/api/computer/status'),
        api<{ enabled: boolean; agents: Agent[] }>('/api/agents').catch(() => ({ enabled: false, agents: [] as Agent[] })),
        api<any>('/api/config/provider').catch(() => null),
        api<any>('/api/workers').catch(() => null),
      ])
      // A newer refresh (or a decision that bumped the generation) started
      // while this one was in flight. Drop it so a late poll cannot restore
      // a pending approval or an old job state.
      if (isStale(gen, refreshGen.current)) return
      setInitialLoaded(true)
      setPollFailed(false)
      // 服务器认下了这个令牌：解锁表单再也不需要出现。
      setCheckingStoredToken(false)
      const taskSessions = taskSessionsOnly(sessionData.sessions)
      // 对话 (webchat) sessions are filtered out of the 任务 list, but a message there still counts as the first message.
      setAnySessionHasMessages(sessionData.sessions.some(s => (s.message_count || 0) > 0 || (s.job_count || 0) > 0))
      setChatSessions(chatSessionsOnly(sessionData.sessions))
      setSessions(taskSessions); setJobs(jobData.jobs); setApprovals(approvalData.approvals)
      setNodes(nodeData.nodes); setSystem(systemData); setComputer(computerData); setAuthenticated(true); setTokenDraft(''); setError('')
      setAgentState(agentData)
      if (providerData) {
        setProviderConfig(providerData.config || providerData)
      }
      if (workersPayload) {
        setWorkersData(workersPayload)
      }
      const pinned = agentData.enabled
        ? (agentData.agents.find(agent => agent.id === (localStorage.getItem('conveyor-agent') || '')) || agentData.agents[0])?.session_id
        : ''
      if (pinned) {
        // An agent's conversation is the session, whether or not it has messages yet.
        if (selectedSessionId !== pinned) { setSelectedSessionId(pinned); setSelectedJobId('') }
        else if (!selectedJobId) {
          const latest = taskSessions.find(item => item.id === pinned)?.latest_job
          if (latest) setSelectedJobId(latest.id)
        }
      } else if (!creatingSession) {
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
    } catch (reason) {
      if (reason instanceof Error && reason.name === 'AbortError') return
      setPollFailed(true)
      // 令牌被拒（或网络不通）：落回解锁表单，连同它的错误提示。
      setCheckingStoredToken(false)
      setError(reason instanceof Error ? reason.message : 'Could not connect')
    }
  }, [api, creatingSession, selectedJobId, selectedSessionId, token])

  // The chat panel reports the conversation it is showing (restored, opened or new),
  // so the 对话 sidebar can highlight it and the session list stays current.
  const handleChatSessionChange = useCallback((sessionId: string) => {
    setChatSessionId(sessionId)
    void refresh()
  }, [refresh])

  const refreshTranscript = useCallback(async () => {
    if (selectedSessionId !== transcriptSessionRef.current) return
    if (!authenticated || !selectedSessionId) {
      sessionDetailFetchGen.current++; setTranscript([]); setTranscriptError(false); setSelectedSessionDetail(null); setSessionDetailError(''); return
    }
    const pending = transcriptRequestRef.current
    if (pending?.sessionId === selectedSessionId && pending.generation === sessionDetailFetchGen.current) return
    const request = { sessionId: selectedSessionId, generation: ++sessionDetailFetchGen.current }
    transcriptRequestRef.current = request
    const controller = new AbortController()
    const timeout = window.setTimeout(() => controller.abort(), 15_000)
    try {
      const session = await api<SessionDetail>(`/api/sessions/${encodeURIComponent(selectedSessionId)}`, { signal: controller.signal })
      if (request.generation === sessionDetailFetchGen.current && selectedSessionId === transcriptSessionRef.current) {
        setTranscript(session.messages || []); setTranscriptError(false); setSelectedSessionDetail(session); setSessionDetailError('')
      }
    } catch {
      if (request.generation === sessionDetailFetchGen.current && selectedSessionId === transcriptSessionRef.current) {
        setTranscriptError(true); setSessionDetailError('暂时读不到这个会话的记录，正在自动重试…')
      }
    } finally {
      window.clearTimeout(timeout)
      if (transcriptRequestRef.current === request) transcriptRequestRef.current = null
    }
  }, [api, authenticated, selectedSessionId])

  useEffect(() => { void refresh() }, [refresh])
  useEffect(() => {
    if (!authenticated || isLockedRef.current) return
    const timer = window.setInterval(() => { void refresh(); void refreshTranscript() }, 3_000)
    pollTimerRef.current = timer
    return () => { window.clearInterval(timer); pollTimerRef.current = null }
  }, [authenticated, refresh, refreshTranscript])

  useEffect(() => {
    const handleOnline = () => { setIsOnline(true); void refresh() }
    const handleOffline = () => setIsOnline(false)
    window.addEventListener('online', handleOnline)
    window.addEventListener('offline', handleOffline)
    return () => {
      window.removeEventListener('online', handleOnline)
      window.removeEventListener('offline', handleOffline)
    }
  }, [refresh])

  useEffect(() => {
    if (!notice) return
    const timer = window.setTimeout(() => setNotice(''), 4000)
    return () => window.clearTimeout(timer)
  }, [notice])

  useEffect(() => {
    if (!agentState.enabled || agentState.agents.length === 0) return
    const params = new URLSearchParams(window.location.search)
    const urlAgent = params.get('agent')
    if (urlAgent) {
      const match = agentState.agents.find(a => a.id === urlAgent)
      if (!match) {
        params.delete('agent')
        const search = params.toString() ? `?${params.toString()}` : ''
        const nextUrl = `${window.location.pathname}${search}${window.location.hash}`
        window.history.replaceState(window.history.state, '', nextUrl)
        const defaultAgent = agentState.agents.find(a => a.is_default) || agentState.agents[0]
        if (defaultAgent) {
          setSelectedAgentId(defaultAgent.id)
          localStorage.setItem('conveyor-agent', defaultAgent.id)
        }
        setNotice('找不到这个助手，已切换到 Conveyor')
      }
    }
  }, [agentState])

  useEffect(() => {
    void refreshTranscript()
  }, [refreshTranscript])

  const fetchApprovalInboxCount = useCallback(async () => {
    if (!system?.features?.approval_inbox || !authenticated || !token || isLockedRef.current) return
    const gen = ++approvalInboxFetchGen.current
    try {
      const res = await fetch('/api/approval-inbox', {
        headers: { Authorization: `Bearer ${token}` },
        signal: pollAbortRef.current.signal,
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
    if (!system?.features?.approval_inbox || !authenticated || isLockedRef.current) return
    void fetchApprovalInboxCount()
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible' && !isLockedRef.current) {
        void fetchApprovalInboxCount()
      }
    }, 5_000)
    approvalTimerRef.current = timer
    return () => { window.clearInterval(timer); approvalTimerRef.current = null }
  }, [fetchApprovalInboxCount, system?.features?.approval_inbox, authenticated])

  useEffect(() => {
    if (!authenticated || !selectedJobId) return
    let stopped = false; let controller: AbortController | null = null; let retry: number | undefined
    let refreshTimer: number | undefined
    // Historical deltas can arrive in hundreds of frames during page load.
    // Coalesce their invalidations instead of issuing seven requests per delta.
    const scheduleRefresh = () => {
      if (refreshTimer !== undefined) return
      refreshTimer = window.setTimeout(() => {
        refreshTimer = undefined
        if (!stopped) { void refresh(); void refreshTranscript() }
      }, 250)
    }
    lastSequence.current = 0; setEvents([]); setRealtimeState('connecting')
    void api<{ events: EventItem[] }>(`/api/jobs/${selectedJobId}/events`).then(({ events: initial }) => {
      if (stopped) return
      setEvents(previous => mergeJobEvents(previous, initial))
      for (const event of initial) lastSequence.current = Math.max(lastSequence.current, event.sequence)
    }).catch(reason => { if (!stopped) setError(reason instanceof Error ? reason.message : String(reason)) })

    const connect = async () => {
      const connection = new AbortController()
      controller = connection
      try {
        const response = await fetch(`/api/events/stream?job_id=${encodeURIComponent(selectedJobId)}&after=${lastSequence.current}`, {
          headers: { Authorization: `Bearer ${token}` }, signal: connection.signal,
        })
        if (!response.ok || !response.body) throw new Error('Realtime unavailable')
        if (stopped) return
        setRealtimeState('live')
        const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = ''
        while (!stopped) {
          const { value, done } = await reader.read(); if (done) break
          buffer += decoder.decode(value, { stream: true })
          const frames = buffer.split('\n\n'); buffer = frames.pop() || ''
          for (const frame of frames) {
            const line = frame.split('\n').find(part => part.startsWith('data: ')); if (!line) continue
            const event = JSON.parse(line.slice(6)) as EventItem
            lastSequence.current = Math.max(lastSequence.current, event.sequence)
            setEvents(previous => mergeJobEvents(previous, [event]))
            if (shouldRefreshForEvent(event.kind)) {
              scheduleRefresh()
            }
          }
        }
      } catch { /* reconnect below unless the selection changed */ }
      finally { connection.abort() }
      if (!stopped) {
        setRealtimeState('reconnecting')
        retry = window.setTimeout(() => void connect(), 1500)
      }
    }
    void connect()
    return () => {
      stopped = true; controller?.abort()
      if (retry) window.clearTimeout(retry)
      if (refreshTimer !== undefined) window.clearTimeout(refreshTimer)
    }
  }, [api, authenticated, refresh, refreshTranscript, selectedJobId, token])

  // Old runs may be older than the global /api/jobs page. Their session
  // history remains available independently of global queue pagination.
  const sessionDetail = selectedSessionDetail?.id === selectedSessionId ? selectedSessionDetail : null
  const selectedJob = useMemo(
    () => (sessionDetail?.jobs || []).find(job => job.id === selectedJobId)
      || jobs.find(job => job.id === selectedJobId),
    [jobs, selectedJobId, sessionDetail],
  )
  const selectedSession = useMemo(() => sessions.find(session => session.id === selectedSessionId), [sessions, selectedSessionId])
  useEffect(() => {
    if (!selectedJob || creatingSession) return
    const matching = sessions.find(session => session.channel === selectedJob.channel
      && session.operator_id === selectedJob.operator_id
      && session.source_chat_id === selectedJob.chat_id)
    const target = matching?.id || selectedJob.chat_id
    if (agentSessionRef.current) return
    if (target && target !== selectedSessionId) setSelectedSessionId(target)
  }, [creatingSession, selectedJob, selectedSessionId, sessions])
  // Apply/Discard from Changes always targets the latest job in the active
  // session chain. When an older run is selected, its approval still belongs
  // to this session and must remain visible; matching only selectedJobId hid
  // the pending decision from the operator.
  const sessionRunIds = useMemo(() => new Set([
    ...(sessionDetail?.runs || []).map(run => run.id),
    sessionDetail?.active_refinement?.latest_queue_job_id,
    selectedSession?.active_refinement?.latest_queue_job_id,
  ].filter((id): id is string => Boolean(id))), [selectedSession, sessionDetail])
  const pendingForJob = approvals.filter(item =>
    (!item.kind || item.kind === 'job')
    && Boolean(item.job_id && (item.job_id === selectedJobId || sessionRunIds.has(item.job_id)))
  )
  const pendingToolApprovals = approvals.filter(item => item.kind === 'tool' && !isChatSessionId(item.session_id || '') && (!selectedSessionId || item.session_id === selectedSessionId))
  const runtimeOwner = runtimeOwnerFromJob(selectedJob)
  const toolEvents = useMemo(() => events.filter(item => item.kind.startsWith('tool.')), [events])
  // started / output / completed events of one call collapse into one row.
  const toolCalls = useMemo(() => groupToolCalls(toolEvents), [toolEvents])
  // The SQLite session chain is authoritative, not the selected job's old
  // events. Viewing turn 1 must not make an already-applied chain look active.
  const activeRefinement = sessionDetail?.active_refinement || selectedSession?.active_refinement || null
  const refinementTurn = activeRefinement?.turn_count || 0
  const runsNewestFirst = useMemo(() => {
    const runs = sessionDetail?.runs || []
    const at = (value?: string) => Date.parse(value || '') || 0
    return [...runs].sort((a, b) => at(b.created_at) - at(a.created_at))
  }, [sessionDetail?.runs])

  const runsAutoExpandKey = useMemo(() => {
    if (sessionDetailError) return `err:${selectedSessionId}`
    if (activeRefinement) return `refinement:${selectedSessionId}:${activeRefinement.chain_id}:${activeRefinement.turn_count}`
    const runs = sessionDetail?.runs || []
    const at = (value?: string) => Date.parse(value || '') || 0
    const attentionRuns = runs.filter(r => ['failed', 'interrupted', 'waiting_for_human', 'waiting_user'].includes(r.state))
    const latest = runs.reduce<(typeof runs)[number] | null>(
      (best, r) => (!best || at(r.created_at) > at(best.created_at) ? r : best),
      null,
    )
    // Do not pop open the run list for old failures when a newer run already finished OK.
    const staleFailuresOnly = attentionRuns.length > 0 && latest?.state === 'completed'
    if (attentionRuns.length > 0 && !staleFailuresOnly) {
      return `action:${selectedSessionId}:${attentionRuns.map(r => `${r.id}:${r.state}`).join(',')}`
    }
    if (selectedJob && ['failed', 'interrupted', 'waiting_for_human', 'waiting_user'].includes(selectedJob.state)) {
      return `action:${selectedSessionId}:${selectedJob.id}:${selectedJob.state}`
    }
    if (pendingForJob.length > 0 || pendingToolApprovals.length > 0) {
      const ids = [...pendingForJob, ...pendingToolApprovals].map(a => a.id).join(',')
      return `approval:${selectedSessionId}:${ids}`
    }
    return null
  }, [activeRefinement, pendingForJob, pendingToolApprovals, selectedJob, selectedSessionId, sessionDetail?.runs, sessionDetailError])
  const changedFilesJob = activeRefinement
    ? (sessionDetail?.jobs?.find(job => job.id === activeRefinement.latest_queue_job_id)
      || selectedSession?.latest_job || selectedJob)
    : selectedJob
  const activeChangedFiles = changedFilesJob?.changed_files?.length || 0
  const changesJobId = changedFilesJob?.id || ''
  useEffect(() => {
    let stopped = false
    if (!authenticated || !changesJobId) { setChangesDiff(emptyChangesDiff); return }
    // Keep the old text while the same job's diff refreshes (no flicker on polls).
    setChangesDiff(previous => previous.jobId === changesJobId ? { ...previous, loading: true, failed: false } : { ...emptyChangesDiff, jobId: changesJobId, loading: true })
    void api<DiffResponse>(`/api/jobs/${encodeURIComponent(changesJobId)}/diff`)
      .then(data => { if (!stopped) setChangesDiff(changesDiffFrom(changesJobId, data)) })
      .catch(() => { if (!stopped) setChangesDiff(previous => ({ ...previous, loading: false, failed: !previous.text })) })
    return () => { stopped = true }
  }, [api, authenticated, changesJobId, changedFilesJob?.updated_at, selectedSessionId])
  const changedFiles = changedFilesJob?.changed_files || []
  const changesBusy = Boolean(changedFilesJob && !terminalJobState(changedFilesJob.state))
  // A chain only reads as "in progress" while its latest run is still going.
  // Once that run finishes, the chain is either waiting for Apply/Discard or
  // produced nothing to apply; neither is an in-progress refinement.
  const refinementRunning = Boolean(activeRefinement && changesBusy)
  const refinementAwaitingApply = Boolean(activeRefinement && !changesBusy && activeChangedFiles > 0)
  const refinementNoChanges = Boolean(activeRefinement && !changesBusy && activeChangedFiles === 0)
  const changeApprovalPending = pendingForJob.some(item => item.job_id === changesJobId)
  // What happened to the last Apply/Discard: the decision reply, or the job's own events after a reload.
  const changesEventOutcome = useMemo(() => {
    const done = [...events].reverse().find(item => item.job_id === changesJobId && (item.kind === 'apply.completed' || item.kind === 'discard.completed'))
    return done ? changesOutcome(done.kind.split('.')[0], 'accepted', String(done.payload.result || '')) : null
  }, [changesJobId, events])
  const changesOutcomeNow = changesResult?.jobId === changesJobId ? changesResult.outcome : changesEventOutcome
  const liveAssistantText = useMemo(() => {
    if (!selectedJob || terminalJobState(selectedJob.state)) return ''
    // Ordered concatenation of the deltas: a cumulative snapshot replaces what came
    // before it instead of being appended (Pi re-sends the whole answer so far).
    return streamedText(events
      .filter(item => item.kind === 'assistant.delta')
      .map(item => String(item.payload.text || '')))
  }, [events, selectedJob])
  // A failed/cancelled run that a newer run has replaced is history: it lives in the run list,
  // not in the badge or notice above the newer conversation.
  const [explicitRunId, setExplicitRunId] = useState('')
  const staleTerminalRun = useMemo(() => {
    if (!selectedJob || selectedJob.id === explicitRunId || !terminalJobState(selectedJob.state) || selectedJob.state === 'completed') return false
    const at = (value?: string) => Date.parse(value || '') || 0
    return [...(sessionDetail?.runs || []), ...(sessionDetail?.jobs || [])].some(run => run.id !== selectedJob.id && at(run.created_at) > at(selectedJob.created_at))
  }, [explicitRunId, selectedJob, sessionDetail])
  const promptAlreadyInTranscript = useMemo(() => {
    if (!selectedJob) return true
    return transcript.some(m =>
      m.job_id === selectedJob.id ||
      (m.role === 'user' && m.content.trim() === selectedJob.prompt_preview.trim())
    )
  }, [selectedJob, transcript])
  // Task runs written before the transcript kept them have no rows; show the
  // request and the run's final event from the run itself, so the session
  // still reads correctly after completion and after a reload.
  const runReplyText = useMemo(() => {
    if (!selectedJobId) return ''
    const done = [...events].reverse().find(item => item.job_id === selectedJobId && (item.kind === 'assistant.completed' || item.kind === 'assistant.failed'))
    return done ? String(done.payload.text || '') : ''
  }, [events, selectedJobId])
  const runTurnFallback = useMemo<TranscriptMessage[]>(() => {
    if (!selectedJob || selectedJob.state !== 'completed' || staleTerminalRun) return []
    if (transcript.some(m => m.job_id === selectedJob.id)) return []
    const at = selectedJob.finished_at || selectedJob.updated_at || selectedJob.created_at
    const base = { session_id: selectedSessionId, job_id: selectedJob.id, kind: 'codex', created_at: at }
    const out: TranscriptMessage[] = []
    if (!promptAlreadyInTranscript && selectedJob.prompt_preview) {
      out.push({ ...base, id: `run-${selectedJob.id}-request`, role: 'user', content: selectedJob.prompt_preview })
    }
    if (runReplyText.trim()) {
      out.push({ ...base, id: `run-${selectedJob.id}-reply`, role: 'assistant', content: runReplyText })
    }
    return out
  }, [promptAlreadyInTranscript, runReplyText, selectedJob, selectedSessionId, staleTerminalRun, transcript])

  useEffect(() => {
    const node = streamRef.current
    if (node) node.scrollTo({ top: node.scrollHeight, behavior: 'smooth' })
  }, [events.length, transcript.length, selectedJobId])

  async function openSettings() {
    setSettingsOpen(true); setError('')
    try {
      const p = await api<any>('/api/config/provider')
      if (p) setProviderConfig(p.config || p)
    }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Could not load settings') }
  }

  // The draft is cleared once the server accepts it; if the run then fails, give it back.
  const lastSubmitted = useRef<{ jobId: string; prompt: string; mode: 'run' | 'fix' } | null>(null)
  useEffect(() => {
    const sent = lastSubmitted.current
    if (!sent || selectedJob?.id !== sent.jobId || selectedJob.state !== 'failed') return
    lastSubmitted.current = null
    setPrompt(current => current.trim() ? current : sent.prompt)
    setMode(sent.mode)
  }, [selectedJob])
  const coarsePointer = useMediaQuery('(pointer: coarse)')
  // A phone keyboard has no Enter key: the composer hint drops that part on narrow screens too.
  const narrowComposer = useMediaQuery('(max-width: 640px)')
  const touchComposer = coarsePointer || narrowComposer
  // N24: the newest line of this conversation. When it is a failure (a model call that could
  // not be made), the transcript shows it as an error line with 重试 beside the composer.
  const lastTranscriptMessage = transcript.length ? transcript[transcript.length - 1] : null
  const failureBesideComposer = Boolean(lastTranscriptMessage
    && (lastTranscriptMessage.kind === 'error' || lastTranscriptMessage.content.startsWith('[error]')))

  // One place sends to 任务: the composer's 发送 and the 重试 button on a failure line (N24).
  async function sendPrompt(text: string, executionOnlySend = executionOnly) {
    const body = text.trim()
    if (!body || busy) return
    setBusy(true); setError(''); setSubmitError(null)
    try {
      const result = await api<{ job_id: string; session_id?: string }>('/api/tasks', {
        method: 'POST', body: JSON.stringify({ prompt: body, mode, session_id: selectedSessionId || undefined, execution_only: executionOnlySend }),
      })
      lastSubmitted.current = result.job_id ? { jobId: result.job_id, prompt: body, mode } : null
      setPrompt('')
      setExplicitRunId('')
      setExecutionOnly(false)
      setCreatingSession(false)
      if (result.session_id) setSelectedSessionId(result.session_id)
      setSelectedJobId(result.job_id || '')
      await Promise.all([refresh(), refreshTranscript()])
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : 'Submit failed'
      // The server's own reason (Chinese when it has one) belongs next to the composer,
      // not only in the banner at the top of the page (N17).
      setSubmitError(shellErrorText(message))
      setError(message)
    }
    finally { setBusy(false) }
  }
  async function submit(event: FormEvent) {
    event.preventDefault()
    await sendPrompt(prompt)
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
    try {
      const result = await api<{ job_id?: string; action?: string; status?: string; result?: string }>(path, { method: 'POST', body: JSON.stringify(body) })
      const outcome = decided && result && (result.action === 'apply' || result.action === 'discard') ? changesOutcome(result.action, result.status || '', result.result || '') : null
      if (outcome) setChangesResult({ jobId: result.job_id || '', outcome })
      await refresh(); if (decided) { void fetchApprovalInboxCount(); void refreshTranscript() }
    }
    catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Action failed')
      // The optimistic drop may have been wrong (network error). Re-read.
      try { await refresh() } catch { /* status already reported */ }
    }
    finally { setBusy(false) }
  }
  async function captureHostScreen() {
    if (screenBusy || !computer?.screen_preview_enabled) return
    setScreenBusy(true); setScreenError(null)
    try {
      const result = await api<{ request: NonNullable<ComputerStatus['screen_request']> }>('/api/computer/screenshot', {
        method: 'POST', body: JSON.stringify({}),
      })
      setComputer(previous => previous ? { ...previous, screen_request: result.request } : previous)
      await refresh()
    } catch (reason) {
      setScreenError(describeFailure(reason instanceof Error ? reason.message : '', '没能请求主机截图，请稍后再试。'))
    } finally { setScreenBusy(false) }
  }
  function unlock(event: FormEvent) {
    event.preventDefault(); const value = tokenDraft.trim(); if (!value) return
    isLockedRef.current = false
    pollAbortRef.current = new AbortController()
    sessionStorage.setItem('conveyor-token', value)
    try { if (rememberToken) localStorage.setItem('conveyor-token', value); else localStorage.removeItem('conveyor-token') } catch { /* storage unavailable */ }
    // The draft stays until the server accepts it, so a rejected token can be corrected in place.
    setToken(value)
  }
  const selectAgent = (agent: Agent) => {
    setSelectedAgentId(agent.id)
    localStorage.setItem('conveyor-agent', agent.id)
    selectSession(agent.session_id)
    setView('chat')
    setSessionsDrawerOpen(false)
  }
  // A conversation found by 记忆 → 「搜索过去的对话」 opens where the 对话 / 任务 lists
  // would open it: an agent's conversation on its 对话 page, a web chat in 对话,
  // anything else (task runs, Telegram, 飞书) in 任务.
  const openSearchedSession = (sessionId: string) => {
    if (!sessionId) return
    historyModeRef.current = 'push'
    if (isSupervisorWorktreeSessionId(sessionId)) { setView('code'); return }
    const agent = agentState.enabled ? agentState.agents.find(item => item.session_id === sessionId) : undefined
    if (agent) { selectAgent(agent); return }
    if (isChatSessionId(sessionId) && !agentState.enabled) {
      setChatOpenRequest(previous => ({ id: sessionId, nonce: (previous?.nonce ?? 0) + 1 }))
      setView('chat')
      return
    }
    const known = sessions.find(item => item.id === sessionId)
    selectSession(sessionId, known?.latest_job?.id)
    setView('tasks')
  }
  const saveAgent = async (draft: AgentDraft, agent?: Agent) => {
    const saved = await api<Agent>(agent ? `/api/agents/${encodeURIComponent(agent.id)}` : '/api/agents', {
      method: agent ? 'PUT' : 'POST', body: JSON.stringify(draft),
    })
    if (!agent) { setSelectedAgentId(saved.id); localStorage.setItem('conveyor-agent', saved.id); selectSession(saved.session_id); setView('chat') }
    await refresh()
  }
  const archiveAgent = async (agent: Agent) => {
    await api(`/api/agents/${encodeURIComponent(agent.id)}`, { method: 'DELETE' })
    if (selectedAgentId === agent.id) { setSelectedAgentId(''); localStorage.removeItem('conveyor-agent') }
    await refresh()
  }
  function lock() {
    isLockedRef.current = true
    pollAbortRef.current.abort()
    pollAbortRef.current = new AbortController()

    if (pollTimerRef.current !== null) { window.clearInterval(pollTimerRef.current); pollTimerRef.current = null }
    if (approvalTimerRef.current !== null) { window.clearInterval(approvalTimerRef.current); approvalTimerRef.current = null }
    if (inboxTimerRef.current !== null) { window.clearInterval(inboxTimerRef.current); inboxTimerRef.current = null }
    if (goalsTimerRef.current !== null) { window.clearInterval(goalsTimerRef.current); goalsTimerRef.current = null }
    if (staleTimerRef.current !== null) { window.clearTimeout(staleTimerRef.current); staleTimerRef.current = null }

    // With PWA disabled, locking retains exactly the legacy console behaviour.
    if (system?.features?.pwa) void leavePwa(token).catch(() => undefined)
    forgetToken(); sessionStorage.removeItem('conveyor-token')
    setToken(''); setAuthenticated(false); setRememberToken(false)
  }

  // ② 算的是「服务器上任何一个能用的对话模型」：模型设置里存下来的，或者服务器用环境
  // 变量自己配好的（GET /api/config/provider 与 /api/system/status 都会说明这件事）。
  const modelConfigured = Boolean(
    (providerConfig && providerConfig.api_key_configured && providerConfig.model)
    || providerConfig?.chat_model_configured
    || system?.features?.chat_model_configured
  )
  const workersEnabled = Boolean(system?.features?.workers)
  const hasEligibleWorker = Boolean(
    workersData?.active ||
    (Array.isArray(workersData?.workers) && workersData.workers.some((w: any) =>
      w.enabled && w.installed && w.runnable && w.auth !== 'needs_login' && w.quota?.state !== 'cooling'
    ))
  )
  // The built-in Pi has no login of its own: it works as soon as 模型设置 has a provider, so
  // checklist step ① says that instead of 「并登录」 (QA N28).
  const builtinPiInstalled = Boolean(
    Array.isArray(workersData?.workers) && workersData.workers.some((w: any) =>
      w.id === 'pi' && w.installed && w.enabled !== false
    )
  )
  // Step ③ ticks from data that survives a reload (any session with messages, or a
  // job) plus the message being sent right now, so it never depends on one poll.
  const hasSentFirstMessage = Boolean(
    chatMessageSent ||
    anySessionHasMessages ||
    (sessions && sessions.length > 0 && sessions.some(s => (s.message_count || 0) > 0 || (s.job_count || 0) > 0)) ||
    (jobs && jobs.length > 0) ||
    (transcript && transcript.length > 0)
  )
  const hasNoUsableWay = Boolean(initialLoaded && !modelConfigured && (!workersEnabled || !hasEligibleWorker))
  // ① only asks for work when the 执行者 feature is on; otherwise the owner cannot act on it.
  const executorStepDone = !workersEnabled || hasEligibleWorker
  const onboardingStepsOpen = Boolean(hasNoUsableWay || !executorStepDone || !modelConfigured || !hasSentFirstMessage)

  const ONBOARDING_DISMISSED_KEY = 'conveyor-onboarding-dismissed'
  const ONBOARDING_HEALTHY_KEY = 'conveyor-onboarding-healthy'

  useEffect(() => {
    if (!initialLoaded) return
    if (hasNoUsableWay) {
      try {
        if (localStorage.getItem(ONBOARDING_HEALTHY_KEY) === 'true') {
          localStorage.removeItem(ONBOARDING_DISMISSED_KEY)
          localStorage.removeItem(ONBOARDING_HEALTHY_KEY)
          setChecklistDismissed(false)
        }
      } catch { /* ignore */ }
    } else {
      try {
        localStorage.setItem(ONBOARDING_HEALTHY_KEY, 'true')
      } catch { /* ignore */ }
    }
  }, [hasNoUsableWay, initialLoaded])

  const handleDismissChecklist = useCallback(() => {
    setChecklistDismissed(true)
    try {
      localStorage.setItem(ONBOARDING_DISMISSED_KEY, 'true')
    } catch { /* ignore */ }
  }, [])

  // N2: when the last step finishes on this page the card is shown once more, all three ticked
  // with 「都准备好了」, so the owner actually sees ③ tick. A reload (or 关闭) hides it for good.
  // Declared above every early return: hooks must run on every render of this component.
  const stepsOpenAtLoadRef = useRef<boolean | null>(null)
  const [checklistFinished, setChecklistFinished] = useState(false)
  useEffect(() => {
    if (!initialLoaded || checklistDismissed) return
    // Whether the steps were still open when this page opened: only then is there a
    // completed state worth showing once. A page that opens already finished shows nothing.
    if (stepsOpenAtLoadRef.current === null) stepsOpenAtLoadRef.current = onboardingStepsOpen
    if (!stepsOpenAtLoadRef.current) return
    if (!onboardingStepsOpen) setChecklistFinished(true)
  }, [checklistDismissed, initialLoaded, onboardingStepsOpen])

  // 带着记住的令牌刷新时，先显示「正在打开…」；只有真的没有令牌、或者令牌被拒了，
  // 才亮出解锁表单（连同它的错误提示）。
  if (!authenticated) {
    if (checkingStoredToken) return <OpeningScreen />
    return <UnlockScreen
      tokenDraft={tokenDraft} onTokenDraftChange={value => { setTokenDraft(value); if (error) setError('') }}
      rememberToken={rememberToken} onRememberTokenChange={setRememberToken}
      error={error} canRetry={Boolean(token)} onRetry={() => { setError(''); void refresh() }}
      onSubmit={event => {
        // Submitting the same token again (e.g. after a network error) must retry, not do nothing.
        const retry = Boolean(tokenDraft.trim()) && tokenDraft.trim() === token
        setError(''); unlock(event)
        if (retry) void refresh()
      }}
    />
  }

  // Derived from server state on every load (no in-memory flag): the checklist stays until every step is done or it is dismissed.
  const showChecklist = initialLoaded && !checklistDismissed
    && (onboardingStepsOpen || (checklistFinished && stepsOpenAtLoadRef.current === true))

  const checklistNode = showChecklist ? (
    <SetupChecklist
      workersEnabled={workersEnabled}
      builtinPiInstalled={builtinPiInstalled}
      hasEligibleWorker={hasEligibleWorker}
      modelConfigured={modelConfigured}
      hasSentFirstMessage={hasSentFirstMessage}
      allDone={checklistFinished}
      onNavigateWorkers={() => {
        historyModeRef.current = 'push'
        setView('workers')
      }}
      onOpenSettings={() => void openSettings()}
      onDismiss={handleDismissChecklist}
    />
  ) : null

  const conversationProps: ChatPanelProps = {
    token, active: view === 'chat',
    onPrepareTask: suggestion => {
      selectSession(suggestion.session_id)
      setCreatingSession(true)
      setPrompt(suggestion.prompt)
      setMode('run')
      setExecutionOnly(true)
      setView('tasks')
    },
    onApprovalDecided: () => { void refresh(); void fetchApprovalInboxCount() },
    onSessionChange: handleChatSessionChange, initialInput: chatDraft,
    onInitialInputConsumed: () => setChatDraft(''),
    // A 会话 sidebar row in 对话 asks the panel to open that conversation.
    openSessionRequest: chatOpenRequest,
    // Sending from 对话 counts as the first message immediately (step ③ of the checklist).
    onMessageSent: () => setChatMessageSent(true),
    setupChecklist: view === 'chat' ? checklistNode : undefined,
  }

  // Shell values for this render: right panel, connection, navigation groups and header text.
  const contextAvailable = view === 'chat' || view === 'tasks'
  const contextPanelOpen = contextPref ? contextPref === 'open' : wideViewport
  const contextShown = contextAvailable && contextPanelOpen
  const toggleContextPanel = (event: React.MouseEvent<HTMLButtonElement>) => {
    if (narrowViewport) { drawerOpenerRef.current = event.currentTarget; setSessionsDrawerOpen(false); setContextDrawerOpen(true); return }
    const next = contextPanelOpen ? 'closed' : 'open'
    setContextPref(next)
    try { localStorage.setItem('conveyor-context-panel', next) } catch { /* storage unavailable */ }
  }
  const openAgentDrawer = (event: React.MouseEvent<HTMLButtonElement>) => {
    drawerOpenerRef.current = event.currentTarget
    setContextDrawerOpen(false)
    setSessionsDrawerOpen(true)
  }
  // Close button of the phone 助手 / 会话 drawer; it sits in the list heading (hidden on desktop).
  const listDrawerClose = <button type="button" className="drawer-close-btn" aria-label="关闭" onClick={() => setSessionsDrawerOpen(false)}>×</button>
  const connection: ConnectionState = !isOnline
    ? 'offline'
    : !initialLoaded
      ? (pollFailed ? 'reconnecting' : 'connecting')
      : (pollFailed || syncStale)
        ? 'reconnecting'
        : (selectedJobId && realtimeState !== 'live')
          ? realtimeState
          : 'live'
  const runningCount = system?.queue.states.running || 0
  const errorMessage = error ? shellErrorText(error) : null
  const mainNav = navItems.filter(item => item.group === 'main')
  const moreNav = navItems.filter(item => item.group === 'more')
  const bottomNav = mainNav.slice(0, 4)
  const sheetNav = navItems.filter(item => !bottomNav.includes(item))
  const shortcutLabel = (target: ShellView) => {
    const index = shortcutViewsRef.current.indexOf(target)
    return index >= 0 ? `${SHORTCUT_PREFIX}${index + 1}` : ''
  }
  const navButton = (item: (typeof navItems)[number], inMenu = false) => {
    const shortcut = shortcutLabel(item.view)
    return <button key={item.view} type="button" className={`shell-nav-item ${view === item.view ? 'active' : ''}`}
      aria-current={view === item.view ? 'page' : undefined}
      title={shortcut ? `${VIEW_LABELS[item.view]}（${shortcut}）` : VIEW_LABELS[item.view]}
      onClick={() => { setView(item.view); setNavMenuOpen(false) }}>
      {inMenu && <ShellIcon name={item.view} size={16} />}
      <span>{VIEW_LABELS[item.view]}</span>
      {item.badge ? <span className="shell-nav-badge" role={item.badgeLabel ? 'img' : undefined} aria-label={item.badgeLabel}>{item.badge > 99 ? '99+' : item.badge}</span> : null}
      {inMenu && shortcut && <kbd aria-hidden="true">{shortcut}</kbd>}
    </button>
  }
  const agentRole = selectedAgent ? [selectedAgent.is_default ? '默认助手' : '助手', agentTag(selectedAgent)].filter(Boolean).join(' · ') : ''
  const scopedAgent = selectedAgent && !selectedAgent.is_default ? selectedAgent : undefined
  const taskTitle = (selectedSession?.title && selectedSession.title !== '新会话' ? selectedSession.title : '')
    || selectedSession?.latest_job?.prompt_preview?.trim() || selectedJob?.prompt_preview?.trim() || '新会话'
  const headerTitle = view === 'chat' ? (selectedAgent?.name || VIEW_LABELS.chat)
    : view === 'tasks' ? (executionOnly ? '继续聊天任务' : creatingSession ? '新会话' : taskTitle)
    : VIEW_LABELS[view]
  const headerSubtitle = view === 'chat' && agentRole ? agentRole
    : view === 'memory' && scopedAgent ? `${scopedAgent.name} 长期记住的信息`
    : view === 'inbox' && scopedAgent ? `${scopedAgent.name} 的定时任务结果和通知`
    : VIEW_SUBTITLES[view]

  return <main className={`app-shell ${mobileUiOn ? 'mobile-ui' : ''}`} data-view={view}>
    <header className="topbar">
      <div className="brand"><span className="brand-mark small">C</span><div><strong>Conveyor</strong><small>控制台</small></div></div>
      <nav className="shell-nav" aria-label="主导航">
        <div className="shell-nav-group">{mainNav.map(item => navButton(item))}</div>
        <span className="shell-nav-divider" aria-hidden="true" />
        <div className="shell-nav-group shell-nav-group--more">{moreNav.map(item => navButton(item))}</div>
        <div className="shell-nav-more" ref={navMoreRef}>
          <button type="button" className={`shell-nav-item ${moreNav.some(item => item.view === view) ? 'active' : ''}`}
            aria-expanded={navMenuOpen} aria-controls="shell-nav-menu" onClick={() => setNavMenuOpen(open => !open)}>
            <span>更多</span><ShellIcon name="chevron" size={14} />
          </button>
          {navMenuOpen && <div className="shell-nav-menu" id="shell-nav-menu" role="group" aria-label="更多页面">
            {moreNav.map(item => navButton(item, true))}
          </div>}
        </div>
      </nav>
      <div className="top-actions">
        {system?.features?.pwa && <PwaNotifications token={token} />}
        <div className="top-status" data-connection={connection}
          title={`${connection === 'live' ? '已连接服务器' : connection === 'connecting' ? '正在连接实时进度' : connection === 'offline' ? '网络已断开' : '连接中断，正在重新连接'}${system?.queue.paused ? ' · 队列已暂停' : ''}`}>
          <span className="live-dot" aria-hidden="true" />
          <span className="top-status-label">{CONNECTION_LABELS[connection]}</span>
          <span className="separator" aria-hidden="true" />
          {runningCount > 0 && <span className="top-status-running">运行中 {runningCount}</span>}
          <span className="top-status-queue">队列 {system?.queue.depth ?? 0}{system?.queue.paused ? '（已暂停）' : ''}</span>
        </div>
        <button type="button" className="settings-button" onClick={() => void openSettings()}><ShellIcon name="settings" size={16} /><span>设置</span></button>
        <button type="button" className="shell-lock-button" onClick={() => setLockConfirmOpen(true)} title="锁定控制台（需确认）"><ShellIcon name="lock" size={16} /><span>锁定</span></button>
      </div>
    </header>
    {errorMessage && <div className="error-banner global" aria-live="polite">
      <span className="shell-error-text">{errorMessage.text}{errorMessage.detail && <small>{errorMessage.detail}</small>}</span>
      <button type="button" aria-label="关闭提示" onClick={() => setError('')}>×</button>
    </div>}
    {notice && <div className="shell-notice global" role="status" aria-live="polite">
      <span>{notice}</span>
      <button type="button" aria-label="关闭提示" onClick={() => setNotice('')}>×</button>
    </div>}
    <section className={`workspace ${contextShown ? 'shell-context-open' : 'shell-context-closed'}`}>
      <aside ref={sessionsDrawerRef} id="shell-side-panel" tabIndex={sessionsDrawerOpen ? -1 : undefined} role={sessionsDrawerOpen ? 'dialog' : undefined} aria-modal={sessionsDrawerOpen ? true : undefined} aria-label={agentState.enabled ? '助手' : '会话'} className={`sessions-panel panel ${sessionsDrawerOpen ? 'drawer-open' : ''}`}>
        {/* In the phone drawer the list heading doubles as the drawer header (title, 新建, close). */}
        {agentState.enabled
          ? <AgentList agents={agentState.agents} selectedId={selectedAgent?.id || ''} onSelect={selectAgent} onNew={() => { setSessionsDrawerOpen(false); setAgentDialog({}) }} onEdit={agent => { setSessionsDrawerOpen(false); setAgentDialog({ agent }) }} headerEnd={listDrawerClose} />
          : <>
        <div className="panel-heading session-list-heading"><h2>会话</h2><button type="button" className="agent-new-btn" onClick={() => { selectSession('', ''); setCreatingSession(true); setPrompt(''); setSessionsDrawerOpen(false); }} aria-label="新建会话" title="新建会话"><span aria-hidden="true">+</span> 新建</button>{listDrawerClose}</div>
        <div className="session-list">
          {sidebarSessions.map(session => (
            <div key={session.id} className="session-item-row">
              <button
                className={`session-item ${session.id === sidebarActiveId ? 'active' : ''}`}
                title={sessionLabel(session)}
                aria-pressed={session.id === sidebarActiveId}
                onClick={() => { openConversation(session); setSessionsDrawerOpen(false); }}
              >
                <span className={`status-rail ${session.latest_job?.state || ''}`} />
                <span>
                  <span className="session-item-title"><strong>{sessionListTitle(session)}</strong><time dateTime={session.last_activity} title={formatTime(session.last_activity)}>{relativeTime(session.last_activity)}</time></span>
                  <small>{session.message_count ?? 0} 条消息 · {session.job_count} 次运行</small>
                </span>
              </button>
              <button
                type="button"
                className="session-archive-btn"
                title="归档会话：从列表中移除，记录仍保留"
                aria-label="归档会话"
                onClick={(e) => { e.stopPropagation(); void archiveSession(session.id) }}
              >
                ×
              </button>
            </div>
          ))}
          {!sidebarSessions.length && <Empty text="还没有会话，点“新建”开始。" />}
        </div>
          </>}
        <QueueSummary states={system?.queue.states} />
      </aside>

      <section className="stream-panel panel">
        <div className={`stream-header ${!mobileUiOn ? 'stream-header-standard' : ''} ${view === 'chat' ? 'stream-header-chat' : ''}`}>
          {narrowViewport && <button type="button" className="shell-header-button shell-agent-switch" onClick={openAgentDrawer}
            aria-haspopup="dialog" aria-expanded={sessionsDrawerOpen} aria-controls="shell-side-panel"
            aria-label={selectedAgent ? `切换助手，当前：${selectedAgent.name}` : '会话列表'}>
            {selectedAgent ? <AgentAvatar agent={selectedAgent} size={22} /> : <ShellIcon name="agents" size={18} />}
            <span>{selectedAgent ? selectedAgent.name : '会话'}</span>
            <ShellIcon name="chevron" size={14} />
          </button>}
          <div className="shell-heading">
            <h2 title={headerTitle}>{headerTitle}</h2>
            {view === 'tasks' && selectedJob && !staleTerminalRun && <StatusBadge state={selectedJob.state} />}
            {headerSubtitle && <p className="shell-subtitle">{headerSubtitle}</p>}
          </div>
          <div className="shell-header-actions">
            {contextAvailable && <button type="button" className={`shell-header-button shell-context-toggle ${!narrowViewport && contextPanelOpen ? 'active' : ''}`}
              onClick={toggleContextPanel} aria-controls="shell-context-panel"
              aria-expanded={narrowViewport ? contextDrawerOpen : contextPanelOpen}
              title={narrowViewport ? '查看详情和改动' : contextPanelOpen ? '收起右侧详情栏' : '展开右侧详情栏'}>
              <ShellIcon name="panel" size={16} /><span>详情</span>
            </button>}
          </div>
        </div>
        <div className="stream-chat-column" style={{ display: view === 'chat' ? 'flex' : 'none', flexDirection: 'column', minWidth: 0, minHeight: 0, height: '100%', flex: '1 1 auto', overflow: 'hidden' }} aria-hidden={view !== 'chat'}>
          {selectedAgent
            ? <AgentWorkspace key={selectedAgent.id} agent={selectedAgent} agents={agentState.agents} onSelectAgent={selectAgent} {...conversationProps} />
            : <ChatPanel key="free-chat" {...conversationProps} />}
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
        ) : view === 'browser' ? (
          <BrowserApprovalsPanel token={token} />
        ) : view === 'skills' ? (
          <SkillsPanel
            token={token}
            onUseInChat={(skillSlug) => {
              setChatDraft(`/skill ${skillSlug} `)
              setView('chat')
            }}
          />
        ) : view === 'workflows' ? (
          <WorkflowPanel token={token} />
        ) : view === 'connectors' ? (
          <ConnectorsPanel token={token} />
        ) : view === 'secrets' ? (
          <SecretsPanel token={token} />
        ) : view === 'workers' ? (
          <WorkersPanel token={token} />
        ) : view === 'memory' ? (
          <MemoryPanel key={selectedAgent?.id || 'operator'} token={token} agentId={selectedAgent && !selectedAgent.is_default ? selectedAgent.id : undefined} agentName={selectedAgent?.name} onOpenSession={openSearchedSession} />
        ) : view === 'inbox' ? (
          <InboxPanel
            agentId={selectedAgent && !selectedAgent.is_default ? selectedAgent.id : undefined}
            agentName={selectedAgent?.name}
            token={token}
            webhooksEnabled={Boolean(system?.features?.webhooks)}
            routinesEnabled={Boolean(system?.features?.routines)}
            onUnreadChange={setInboxUnread}
            onApprovalDecided={() => {
              void refresh()
              void fetchApprovalInboxCount()
            }}
          />
        ) : view === 'chat' ? null : view === 'code' ? (
          system && !system.features?.agent_supervisor ? (
            <div className="welcome-state">
              <div className="brand-mark">C</div>
              <h2>Code 工作台尚未启用</h2>
              <p>当前服务器未开启 <code>agent_supervisor</code> 功能开关。</p>
            </div>
          ) : (
            <Suspense fallback={<div className="empty">正在加载项目…</div>}>
              <SupervisorWorkspace
                token={token}
                active={view === 'code'}
                coordinatorFallbackName={providerConfig ? `${providerConfig.provider_name} · ${providerConfig.model}` : selectedAgent?.name}
                onOpenWorkers={workersEnabled ? () => { historyModeRef.current = 'push'; setView('workers') } : undefined}
              />
            </Suspense>
          )
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
            <div className="event-stream task-stream" ref={streamRef}>
              {view === 'tasks' && checklistNode}
              {refinementRunning && <div className="job-notice task-notice task-notice--sticky" role="status"><strong>正在继续修改</strong><span>第 {refinementTurn} 轮还在运行。继续输入会在同一个隔离副本里接着改。</span></div>}
              {refinementAwaitingApply && <div className="job-notice task-notice task-notice--sticky"><strong>改动还没应用</strong><span>已连续修改 {refinementTurn} 轮，{activeChangedFiles} 个文件有改动。继续输入会在同一个隔离副本里接着改；满意后在右侧「改动」里应用或放弃。</span></div>}
              {refinementNoChanges && <div className="job-notice task-notice" role="status"><strong>这次没有改动文件</strong><span>可以在下方重新描述要做的事，再试一次。</span></div>}
              {selectedJob && !terminalJobState(selectedJob.state) && realtimeState === 'reconnecting' && <div className="job-notice task-notice" role="status"><strong>正在重新连接任务进度</strong><span>已收到的记录会保留，连接恢复后继续同步。</span></div>}
              {selectedJob?.state === 'completed' && !transcript.length && !runTurnFallback.length && <div className="job-notice task-notice" role="status"><strong>任务已完成</strong><span>这次运行没有保存文字回复，可以在右侧「运行日志」里查看结果。</span></div>}

              {transcriptError && <div className="job-notice task-notice" role="status"><strong>暂时无法更新会话记录</strong><span>已收到的内容会保留。</span><button type="button" onClick={() => void refreshTranscript()}>重试读取</button></div>}

              {transcript.length > 0 && <TranscriptPanel messages={transcript} agent={selectedAgent} onRetry={question => void sendPrompt(question, false)} />}
              {runTurnFallback.length > 0 && <TranscriptPanel messages={runTurnFallback} agent={selectedAgent} />}

              {selectedJob && selectedJob.prompt_preview && selectedJob.state !== 'completed' && !staleTerminalRun && !promptAlreadyInTranscript && (
                <article className="transcript-message role-user pending-turn">
                  <div className="transcript-content">
                    <p className="formatted-paragraph">{selectedJob.prompt_preview}</p>
                  </div>
                </article>
              )}

              {selectedJob?.state === 'failed' && !staleTerminalRun && (() => {
                const info = describeJobError(selectedJob.error)
                return (
                  <div className="job-notice failed task-notice" role="alert">
                    <strong>任务执行失败</strong>
                    <span>{info.message}</span>
                    {info.retryable !== false && <small className="task-notice-hint">这段内容已留在下方输入框，修改后可以重新发送。</small>}
                    {info.action === 'workers' && (
                      <button
                        type="button"
                        className="task-notice-action"
                        onClick={() => {
                          historyModeRef.current = 'push'
                          setView('workers')
                        }}
                      >
                        {info.actionLabel || '去执行者'}
                      </button>
                    )}
                    {info.action === 'settings' && (
                      <button
                        type="button"
                        className="task-notice-action"
                        onClick={() => void openSettings()}
                      >
                        {info.actionLabel || '去设置'}
                      </button>
                    )}
                    {selectedJob.error && (
                      <details className="task-notice-raw">
                        <summary>详情</summary>
                        <small className="task-notice-detail">{selectedJob.error}</small>
                      </details>
                    )}
                  </div>
                )
              })()}
              {selectedJob?.state === 'cancelled' && !staleTerminalRun && <div className="job-notice task-notice" role="status"><strong>任务已取消</strong><span>已有记录已保留，可以在下方输入下一步。</span></div>}
              {selectedJob?.state === 'interrupted' && !staleTerminalRun && <div className="job-notice task-notice" role="status"><strong>任务已中断</strong><span>已有记录已保留，可以在下方补充说明继续处理。</span>{selectedJob.error && <details className="task-notice-raw"><summary>详情</summary><small className="task-notice-detail">{selectedJob.error}</small></details>}</div>}

              {selectedJob && !terminalJobState(selectedJob.state) && (
                <div className="live-job-turn">
                  <div className={`live-job-banner ${selectedJob.state}`}>
                    <span className={`live-pulse ${selectedJob.state}`} aria-hidden="true" />
                    <span>{selectedJob.state === 'queued' ? '排队中，前面的运行结束后开始' : `${selectedAgent?.name || 'Conveyor'} 正在处理…`}</span>
                    {toolCalls.length > 0 && <span className="live-job-count">已执行 {toolCalls.length} 个操作</span>}
                  </div>

                  {toolCalls.length > 0 && (
                    <div className="live-tools-list">
                      {toolCalls.map(call => (
                        <ToolCallCard key={call.id} call={call} jobFinished={false} time={formatTime(call.timestamp)} />
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

              {selectedJob && terminalJobState(selectedJob.state) && toolCalls.length > 0 && (
                <details className="completed-tools-drawer">
                  <summary className="completed-tools-summary">
                    <span className="tool-summary-icon" aria-hidden="true">⚡</span>
                    <span className="completed-tools-text">
                      <strong>这次运行执行了 {toolCalls.length} 个操作</strong>
                      {toolCalls.some(call => call.status === 'failed') && <span className="completed-tools-failed">其中 {toolCalls.filter(call => call.status === 'failed').length} 个失败</span>}
                    </span>
                    <span className="expand-hint" aria-hidden="true"><span className="when-closed">展开</span><span className="when-open">收起</span></span>
                  </summary>
                  <div className="completed-tools-body">
                    {toolCalls.map(call => (
                      <ToolCallCard key={call.id} call={call} jobFinished time={formatTime(call.timestamp)} />
                    ))}
                  </div>
                </details>
              )}

              {!transcript.length && !selectedJob && (
                <div className="welcome-state task-welcome">
                  <div className="brand-mark">C</div>
                  <h2>要 {selectedAgent?.name || 'Conveyor'} 做什么？</h2>
                  <p>「提问」只回答问题、查资料，不动文件；「改代码」会在一份隔离的项目副本里修改，改完你先看差异，再决定是否应用。</p>
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
            {submitError && (
              <div className="composer task-composer task-composer--notice" role="alert">
                <div className="composer-handoff-note task-composer-error-text">
                  <span>{submitError.text}</span>
                  {submitError.detail && <small>{submitError.detail}</small>}
                </div>
                <div className="task-composer-bar">
                  <button type="button" className="shell-button" onClick={() => setSubmitError(null)}>知道了</button>
                </div>
              </div>
            )}
            {isSupervisorWorktreeSessionId(selectedSessionId) ? (
              // 项目 里的工作目录对话：在这里发送只会被服务器拒绝（409）。说清楚，并给出两条出路。
              <div className="composer task-composer task-composer--notice" role="status">
                <div className="composer-handoff-note task-composer-readonly-text">
                  <span>这是「项目」里的工作目录对话，请到「项目」页讨论或派发。</span>
                </div>
                <div className="task-composer-bar">
                  <button type="button" className="shell-button primary" onClick={() => { historyModeRef.current = 'push'; setView('code') }}>去项目</button>
                  <button
                    type="button"
                    className="shell-button"
                    onClick={() => { selectSession('', ''); setCreatingSession(true); setPrompt(''); setSubmitError(null) }}
                  >
                    新建会话
                  </button>
                </div>
              </div>
            ) : (
            <form className={`composer task-composer ${failureBesideComposer ? 'task-composer--settled' : ''}`} onSubmit={submit}>
              {executionOnly && <div className="composer-handoff-note" role="status">原请求已带入任务页。你可以编辑内容，再点击发送执行。</div>}
              <textarea aria-label="任务内容" value={prompt} onChange={event => { setPrompt(event.target.value); if (submitError) setSubmitError(null) }} placeholder={activeRefinement ? '继续说明要怎么改…' : '描述要做的事…'} rows={2} maxLength={8000} onKeyDown={event => { if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return; if (coarsePointer) return; if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit() } }} />
              <div className="task-composer-bar">
                <div className="mode-switch task-mode-switch" role="group" aria-label="运行方式">
                  <button type="button" className={mode === 'run' ? 'active' : ''} aria-pressed={mode === 'run'} title="提问：只回答问题、查资料，不修改项目文件" onClick={() => setMode('run')}>提问</button>
                  <button type="button" className={mode === 'fix' ? 'active' : ''} aria-pressed={mode === 'fix'} title="改代码：在一份隔离的项目副本里修改。完成后你先查看差异，再决定是否应用改动。" onClick={() => setMode('fix')}>改代码</button>
                </div>
                <span className="task-composer-hint">{mode === 'fix' ? '在隔离副本里改，看过差异再应用' : touchComposer ? '点「发送」提交' : <>Enter 发送<span className="chat-hint-newline"> · Shift + Enter 换行</span></>}</span>
                <button className="send-button" onPointerDown={event => event.preventDefault()} onMouseDown={event => event.preventDefault()} disabled={!prompt.trim() || busy}>{busy ? '发送中…' : '发送'}</button>
              </div>
            </form>
            )}
          </>
        )}
      </section>

      <aside ref={contextDrawerRef} id="shell-context-panel" tabIndex={contextDrawerOpen ? -1 : undefined} role={contextDrawerOpen ? 'dialog' : undefined} aria-modal={contextDrawerOpen ? true : undefined} aria-label="详情" className={`context-panel panel ${contextDrawerOpen ? 'drawer-open' : ''} ${contextShown ? '' : 'shell-context-hidden'}`}>
        <div className="mobile-drawer-header">
          <strong>详情</strong>
          <button type="button" className="drawer-close-btn" aria-label="关闭详情" onClick={() => setContextDrawerOpen(false)}>×</button>
        </div>
        {selectedAgent && <div className="agent-card agent-card--compact">
          <div className="agent-card-header">
            <AgentAvatar agent={selectedAgent} size={40} />
            <div className="agent-card-title">
              <h2>{selectedAgent.name}</h2>
              {agentTag(selectedAgent) && <p className="agent-card-tag" title={selectedAgent.workspace_path}>{agentTag(selectedAgent)}</p>}
            </div>
          </div>
          <div className="agent-tabs" role="tablist" aria-label="助手面板">
            {(['details', 'library', 'computer'] as const).map(tab => (
              <button key={tab} type="button" role="tab" aria-selected={agentTab === tab} className={agentTab === tab ? 'active' : ''} onClick={() => setAgentTab(tab)}>
                {tab === 'details' ? '详情' : tab === 'library' ? '资料' : '电脑'}
              </button>
            ))}
          </div>
        </div>}
        {/* An open human takeover renders here (HumanTakeoverPanel), on every tab. */}
        <div className="takeover-active-slot" />
        {pendingForJob.map(approval => (
          <section className={`approval-card approval-card--${approval.action === 'discard' ? 'discard' : 'apply'}`} key={approval.id}>
            <h3>需要你确认：{approval.action === 'discard' ? '放弃这些改动？' : '应用这些改动？'}</h3>
            <p>{approval.action === 'discard' ? '隔离副本里的改动会被删除，无法恢复。' : '改动会写进项目文件夹，提交前你还可以再检查。'}</p>
            <p className="approval-expiry">只作用于运行 <code>{approval.job_id}</code> · {expiryText(approval.expires_at)}</p>
            <div className="action-row">
              <button type="button" disabled={busy} onClick={() => action(`/api/approvals/${approval.id}/reject`)}>拒绝</button>
              <button type="button" className={approval.action === 'discard' ? 'danger' : 'primary'} disabled={busy} onClick={() => action(`/api/approvals/${approval.id}/approve`)}>批准</button>
            </div>
          </section>
        ))}
        {pendingToolApprovals.map(approval => (
          <section className="approval-card approval-card--tool" key={approval.id}>
            <h3>需要你确认：{toolDisplayName(approval.tool_name)}？</h3>
            {approval.arg && <pre className="approval-arg">{approval.arg}</pre>}
            <p className="approval-expiry">
              {approval.summary && toolDisplayName(approval.tool_name) === approval.tool_name && <>{approval.summary} · </>}
              {approval.tool_name && <><code>{approval.tool_name}</code> · </>}{expiryText(approval.expires_at)}
            </p>
            <div className="action-row">
              <button type="button" disabled={busy} onClick={() => action(`/api/approvals/${approval.id}/reject`)}>拒绝</button>
              <button type="button" className="primary" disabled={busy} onClick={() => action(`/api/approvals/${approval.id}/approve`)}>批准</button>
            </div>
          </section>
        ))}
        {selectedAgent && agentTab === 'library' && <ContextSection title="资料" className="context-section--library" hideTitle>
          <AgentLibrary key={selectedAgent.id} agentId={selectedAgent.id} token={token} />
        </ContextSection>}
        {(!selectedAgent || agentTab === 'details') && <>
        <ContextSection title="改动" className="context-section--changes" collapsible storageKey="conveyor-changes-collapsed" meta={changedFiles.length ? `${changedFiles.length} 个文件` : undefined}>
          {changesOutcomeNow && !changeApprovalPending && <p className={`changes-outcome ${changesOutcomeNow.tone}`} role="status">{changesOutcomeNow.text}{changesOutcomeNow.detail && <small>{changesOutcomeNow.detail}</small>}</p>}
          {!changedFiles.length ? <Empty text={changesBusy && changedFilesJob?.mode === 'fix' ? '正在修改，改动会显示在这里' : '暂无改动'} /> : <>
            {activeRefinement && <p className="changes-note">已连续修改 {refinementTurn} 轮，改动都累积在同一个隔离副本里。</p>}
            <ChangedFileList files={changedFiles} stats={changesDiff.files} />
            <details className="diff-view">
              <summary><span>查看差异</span><DiffStat additions={changesDiff.additions} deletions={changesDiff.deletions} /></summary>
              <DiffBody diff={changesDiff.text} loading={changesDiff.loading} failed={changesDiff.failed} />
            </details>
            {!changesOutcomeNow?.applied && <>
              <div className="action-row changes-actions">
                <button type="button" className="danger" disabled={busy || changesBusy || changeApprovalPending} onClick={() => { setChangesResult(null); void action(`/api/jobs/${changesJobId}/discard`) }}>放弃改动</button>
                <button type="button" className="primary" disabled={busy || changesBusy || changeApprovalPending} onClick={() => { setChangesResult(null); void action(`/api/jobs/${changesJobId}/apply`) }}>应用改动</button>
              </div>
              {(changesBusy || changeApprovalPending) && <p className="changes-hint">{changeApprovalPending ? '已发出确认，请在上方批准或拒绝。' : '运行结束后才能应用或放弃。'}</p>}
            </>}
          </>}
        </ContextSection>
        {selectedAgent && <ContextSection title="助手设定" className="context-section--agent">
          <ClampedText className="agent-instructions" text={selectedAgent.instructions || '还没有设置长期指令。'} lines={4} />
          {selectedAgent.workspace_path && <div className="agent-folder"><span>项目文件夹</span><code title={selectedAgent.workspace_path}>{selectedAgent.workspace_path}</code></div>}
          {selectedAgent.workspace_status === 'missing' && <p className="screen-request-status failed" role="alert">主机上找不到这个文件夹，修好之前这个助手的任务都会失败。</p>}
          {selectedAgent.workspace_status === 'not_git' && <p className="screen-request-status failed" role="alert">这个文件夹不是 Git 仓库的根目录，修好之前这个助手的任务都会失败。</p>}
          {!selectedAgent.workspace_path && !selectedAgent.is_default && <p className="screen-privacy-note">没有设置项目文件夹，任务会在共享工作区里运行。</p>}
          <div className="action-row"><button type="button" onClick={() => setAgentDialog({ agent: selectedAgent })}>编辑助手</button></div>
        </ContextSection>}
        <ContextSection
          title="运行记录"
          className="context-section--runs"
          collapsible
          defaultCollapsed
          resetKey={selectedSessionId}
          autoExpandKey={runsAutoExpandKey}
          meta={sessionDetail?.runs?.length ? `${sessionDetail.runs.length} 条` : (!sessionDetailError ? '这个会话还没有运行' : undefined)}
        >
          {(refinementRunning || refinementAwaitingApply) && <div className="session-chain-summary">
            <strong>{refinementRunning ? `正在继续修改 · 第 ${refinementTurn} 轮` : `修改待应用 · 第 ${refinementTurn} 轮`}</strong>
            <small>这些运行共用一个隔离副本，应用或放弃改动后结束。</small>
          </div>}
          <div className="session-run-list" role="group" aria-label="本会话的运行记录">
            {sessionDetailError
              ? <p className="screen-request-status failed" role="alert">暂时读不到这个会话的运行记录，正在自动重试…</p>
              : <>
                {runsNewestFirst.map(run => (
                  <button key={run.id} type="button"
                    className={`session-run ${run.id === selectedJobId ? 'selected' : ''}`}
                    aria-pressed={run.id === selectedJobId}
                    onClick={() => { setExplicitRunId(run.id); setSelectedJobId(run.id) }}>
                    <span className="session-run-heading">
                      <strong>{run.prompt_preview || run.id}</strong>
                      <span className={`status-rail ${run.state}`} />
                    </span>
                    <small>{modeLabel(run.mode)} · {stateLabel(run.state)} · {formatTime(run.created_at)}
                      {run.refinement_turn ? ` · 第 ${run.refinement_turn} 轮` : ''}
                    </small>
                  </button>
                ))}
                {!sessionDetail?.runs?.length && <Empty text="这个会话还没有运行" />}
              </>}
          </div>
        </ContextSection>
        <ContextSection title="运行详情" className="context-section--job">
          {selectedJob ? <>
            <div className="job-summary">
              <StatusBadge state={selectedJob.state} />
              <span className="job-mode">{modeLabel(selectedJob.mode)}</span>
              {['queued', 'running'].includes(selectedJob.state) && <button type="button" className="job-stop" disabled={busy} title="停止这次运行" onClick={() => action(`/api/jobs/${selectedJob.id}/cancel`)}>停止</button>}
            </div>
            <KeyValue label="开始时间" value={formatTime(selectedJob.started_at || selectedJob.created_at)} />
            {selectedJob.finished_at && <KeyValue label="结束时间" value={formatTime(selectedJob.finished_at)} />}
            {activeRefinement && <KeyValue label="继续修改" value={`第 ${refinementTurn} 轮 · ${refinementRunning ? '进行中' : refinementAwaitingApply ? '待应用' : '没有改动'}`} />}
            <KeyValue label="运行编号" value={selectedJob.id} mono />
            {!terminalJobState(selectedJob.state) && <RuntimeOwnerCard owner={runtimeOwner} state={selectedJob.state} />}
            {events.length > 0 && (
              <details className="telemetry-drawer">
                <summary className="telemetry-summary">
                  <span>运行日志（{events.length} 条）</span>
                  <b aria-hidden="true">⌄</b>
                </summary>
                <div className="telemetry-list">
                  {events.map(ev => (
                    <div key={ev.event_id} className="telemetry-item">
                      <div className="telemetry-item-header">
                        <span className={`telemetry-pill ${ev.kind.split('.')[0]}`}>{eventKindLabel(ev.kind)}</span>
                        <time>{formatTime(ev.timestamp)}</time>
                      </div>
                      {Boolean(ev.payload?.text) && <div className="telemetry-text">{String(ev.payload.text)}</div>}
                      {Boolean(ev.payload?.name) && <div className="telemetry-text">工具：{toolDisplayName(String(ev.payload.name))}</div>}
                      {Boolean(ev.payload?.prompt) && <div className="telemetry-text">请求：{String(ev.payload.prompt).slice(0, 100)}</div>}
                      {Boolean(ev.payload?.result) && <div className="telemetry-text">{String(ev.payload.result).slice(0, 400)}</div>}
                      {Boolean(ev.payload?.error) && <div className="telemetry-error">{String(ev.payload.error)}</div>}
                    </div>
                  ))}
                </div>
              </details>
            )}
          </> : <Empty text="选一次运行查看详情" />}
        </ContextSection>
        </>}
        {(!selectedAgent || agentTab === 'computer') && <>
        <ContextSection title="电脑" className="context-section--computer" hideTitle={Boolean(selectedAgent)}>
          <div className="computer-status-row">
            <div className="computer-status-text">
              <span>自动操作电脑</span>
              <strong className={computer?.armed ? 'on' : ''}>{computer?.armed ? (computer.arm_remaining_seconds > 0 ? `已开启 · 还剩 ${durationText(computer.arm_remaining_seconds)}` : '已开启') : '未开启'}</strong>
            </div>
            <button type="button" className={`emergency ${computer?.armed ? '' : 'is-idle'}`.trim()} title="立即停止助手对电脑的所有自动操作（仅在助手正在操作电脑时需要）" onClick={() => action('/api/computer/stop')}>紧急停止</button>
          </div>
          {computer?.active_task && <KeyValue label="正在执行" value={computer.active_task.status ? stateLabel(String(computer.active_task.status)) : String(computer.active_task.task_id || '进行中')} />}
          <LiveScreenPanel key={selectedAgent?.id || 'host'} token={token} agentId={selectedAgent?.id} agentName={selectedAgent?.name} />
          {/* HumanTakeoverPanel renders the idle 人工接管 card here. */}
          <div className="takeover-slot" />
          {computer?.screen_preview_enabled || computer?.screenshots[0] ? <div className="host-screen-card">
            <div className="host-screen-heading">
              <div><strong>主机截图</strong><small>只读 · 手动截取一张</small></div>
              <button type="button" className="screen-capture-button" disabled={!computer?.screen_preview_enabled || screenBusy || hostScreenRequestPending(computer?.screen_request)} onClick={() => void captureHostScreen()}>
                {screenBusy ? '请求中…' : hostScreenRequestPending(computer?.screen_request) ? '截取中…' : '截取'}
              </button>
            </div>
            {computer?.screenshots[0]
              ? <AuthenticatedImage artifact={computer.screenshots[0]} token={token} />
              : <div className="screen-empty"><span aria-hidden="true">▣</span><strong>还没有截图</strong><p>点「截取」后，Mac 会传来一张缩小的截图，原图留在 Mac 上。</p></div>}
            {!computer?.screen_preview_enabled && <p className="screen-privacy-note">截图分享已关闭，画面只保留在 Mac 上。</p>}
            {computer?.screen_request && <p className={`screen-request-status ${computer.screen_request.status === 'failed' ? 'failed' : ''}`} aria-live="polite">{hostScreenRequestLabel(computer.screen_request)}{hostScreenRequestDetail(computer.screen_request) && <small className="screen-request-detail">{hostScreenRequestDetail(computer.screen_request)}</small>}</p>}
            {screenError && <p className="screen-request-status failed" role="alert">{screenError.text}{screenError.detail && <small className="screen-request-detail">{screenError.detail}</small>}</p>}
          </div> : <p className="screen-privacy-note host-screen-off">主机截图未开启</p>}
          {nodes.length > 0 && <div className="node-list" role="group" aria-label="执行节点">
            <p className="computer-subhead">执行节点</p>
            {nodes.map(node => <div className="node-card" key={node.id}>
              <span className={`node-dot ${node.status}`} aria-hidden="true" />
              <strong>{node.name}</strong>
              <small>{nodeTypeLabel(node.type)} · {nodeStatusLabel(node.status)}{node.status !== 'online' && node.last_seen_at ? ` · 最后在线 ${formatTime(node.last_seen_at)}` : ''}</small>
            </div>)}
          </div>}
          {!nodes.length && <Empty text="没有可用的执行节点" />}
        </ContextSection>
        </>}
        {(!selectedAgent || agentTab === 'details') && <>
        <ContextSection title="系统" className="context-section--system">
          <div className="system-stats">
            <div title={system?.load_average?.map(n => n.toFixed(2)).join(' / ')}><span>负载</span><strong>{system?.load_average?.[0]?.toFixed(2) ?? '—'}</strong></div>
            <div><span>可用内存</span><strong>{bytes(system?.memory?.available ?? null)}</strong></div>
            <div><span>可用磁盘</span><strong>{bytes(system?.disk?.free ?? null)}</strong></div>
          </div>
          <div className="system-channels">
            <span className={system?.channels?.telegram?.configured ? 'on' : ''}>Telegram {system?.channels?.telegram?.configured ? '已配置' : '未配置'}</span>
            <span className={system?.channels?.feishu?.configured ? 'on' : ''}>飞书 {system?.channels?.feishu?.configured ? '已配置' : '未配置'}</span>
          </div>
        </ContextSection>
        </>}
      </aside>
    </section>
    {mobileUiOn && sessionsDrawerOpen && (
      <div className="mobile-backdrop" role="button" aria-label="关闭列表" onClick={() => setSessionsDrawerOpen(false)} />
    )}
    {mobileUiOn && contextDrawerOpen && (
      <div className="mobile-backdrop" role="button" aria-label="关闭详情" onClick={() => setContextDrawerOpen(false)} />
    )}
    {mobileUiOn && moreSheetOpen && (
      <div className="mobile-sheet-overlay" onClick={() => setMoreSheetOpen(false)}>
        <div ref={moreSheetRef} tabIndex={-1} className="mobile-sheet" onClick={e => e.stopPropagation()} role="dialog" aria-modal="true" aria-label="更多">
          <div className="mobile-sheet-header">
            <strong>更多</strong>
            <button type="button" className="drawer-close-btn" aria-label="关闭" onClick={() => setMoreSheetOpen(false)}>×</button>
          </div>
          <div className="mobile-sheet-items">
            {sheetNav.map(item => (
              <button key={item.view} type="button" className={`mobile-sheet-item ${view === item.view ? 'active' : ''}`}
                aria-current={view === item.view ? 'page' : undefined}
                onClick={() => { setView(item.view); setMoreSheetOpen(false) }}>
                <span className="mobile-sheet-label"><ShellIcon name={item.view} size={20} />{VIEW_LABELS[item.view]}</span>
                {item.badge ? <span className="mobile-badge" role={item.badgeLabel ? 'img' : undefined} aria-label={item.badgeLabel}>{item.badge}</span> : null}
              </button>
            ))}
          </div>
          <div className="mobile-sheet-items mobile-sheet-system">
            <button type="button" className="mobile-sheet-item" onClick={() => { setMoreSheetOpen(false); void openSettings() }}>
              <span className="mobile-sheet-label"><ShellIcon name="settings" size={20} />设置</span>
            </button>
            <button type="button" className="mobile-sheet-item" onClick={() => { setMoreSheetOpen(false); setLockConfirmOpen(true) }}>
              <span className="mobile-sheet-label"><ShellIcon name="lock" size={20} />锁定</span>
            </button>
          </div>
        </div>
      </div>
    )}
    {mobileUiOn && (
      <nav className="mobile-bottom-nav" aria-label="底部导航">
        {bottomNav.map(item => (
          <button key={item.view} type="button" className={`mobile-nav-item ${view === item.view && !moreSheetOpen ? 'active' : ''}`}
            aria-current={view === item.view ? 'page' : undefined}
            aria-label={item.badgeLabel ? `${VIEW_LABELS[item.view]}，${item.badgeLabel}` : undefined}
            onClick={() => { setView(item.view); setMoreSheetOpen(false) }}>
            <span className="mobile-nav-icon" style={{ position: 'relative' }}>
              <ShellIcon name={item.view} size={22} />
              {item.badge ? <span className="mobile-badge-pill" role={item.badgeLabel ? 'img' : undefined} aria-label={item.badgeLabel}>{item.badge > 99 ? '99+' : item.badge}</span> : null}
            </span>
            <span>{VIEW_LABELS[item.view]}</span>
          </button>
        ))}
        <button type="button" ref={moreButtonRef}
          className={`mobile-nav-item ${moreSheetOpen || sheetNav.some(item => item.view === view) ? 'active' : ''}`}
          aria-haspopup="dialog" aria-expanded={moreSheetOpen}
          onClick={() => setMoreSheetOpen(prev => !prev)}>
          <span className="mobile-nav-icon"><ShellIcon name="more" size={22} /></span>
          <span>更多</span>
        </button>
      </nav>
    )}
    {lockConfirmOpen && <LockConfirmDialog onCancel={() => setLockConfirmOpen(false)} onConfirm={() => { setLockConfirmOpen(false); lock() }} />}
    {agentDialog && <AgentDialog
      agent={agentDialog.agent}
      existingAgents={agentState.agents}
      onClose={() => setAgentDialog(null)}
      onSave={draft => saveAgent(draft, agentDialog.agent)}
      onArchive={agentDialog.agent ? () => archiveAgent(agentDialog.agent as Agent) : undefined}
    />}
    {settingsOpen && <ProviderSettings
      config={providerConfig} loadError={providerConfig ? '' : error}
      onRetry={() => void openSettings()} onClose={() => setSettingsOpen(false)}
      onSave={async payload => {
        const result = await api<{ config: ProviderConfig }>('/api/config/provider', { method: 'POST', body: JSON.stringify(payload) })
        setProviderConfig(result.config)
      }} />}
  </main>
}

/** The api() helper's code for a 401, also when it arrives as "Error: Token rejected". */
function isTokenRejected(message: string) {
  return /^(?:(?:Type)?Error:\s*)?token rejected$/i.test(message.trim())
}

/** A failed request → one short Chinese sentence, plus a muted technical detail when it helps. */
function describeFailure(message: string, fallback: string): { text: string; detail?: string } {
  const raw = message.trim().replace(/^(?:Type)?Error:\s*/, '')
  if (/failed to fetch|networkerror|load failed|network request failed/i.test(raw)) return { text: '连不上服务器，请检查网络后重试。' }
  if (isTokenRejected(raw)) return { text: '访问令牌不正确，请检查后重试。' }
  const status = raw.match(/^request failed \((\d{3})\)$/i)
  if (status) return { text: Number(status[1]) >= 500 ? '服务器暂时出了问题，请稍后再试。' : fallback, detail: `HTTP ${status[1]}` }
  if (/internal error/i.test(raw)) return { text: '服务器暂时出了问题，请稍后再试。', detail: raw }
  return { text: fallback, detail: raw || undefined }
}

const DIALOG_FOCUSABLE = 'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [href], [tabindex]:not([tabindex="-1"])'
/** Keep Tab inside an open dialog. */
function keepTabInside(event: React.KeyboardEvent<HTMLElement>) {
  if (event.key !== 'Tab') return
  const items = Array.from(event.currentTarget.querySelectorAll<HTMLElement>(DIALOG_FOCUSABLE)).filter(item => item.offsetParent !== null)
  if (!items.length) { event.preventDefault(); return }
  const first = items[0]; const last = items[items.length - 1]
  if (event.shiftKey && (document.activeElement === first || document.activeElement === event.currentTarget)) { event.preventDefault(); last.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
}

/** 令牌正在被服务器确认时的那一秒：只有一个牌子，没有任何输入框。 */
function OpeningScreen() {
  return <main className="unlock-shell">
    <div className="unlock-card unlock-card--opening">
      <div className="brand-mark" aria-hidden="true">C</div>
      <p className="unlock-lead" role="status">正在打开…</p>
    </div>
  </main>
}

function UnlockScreen({ tokenDraft, onTokenDraftChange, rememberToken, onRememberTokenChange, error, canRetry, onRetry, onSubmit }: {
  tokenDraft: string; onTokenDraftChange: (value: string) => void
  rememberToken: boolean; onRememberTokenChange: (value: boolean) => void
  error: string; canRetry: boolean; onRetry: () => void
  onSubmit: (event: FormEvent) => void
}) {
  const [showToken, setShowToken] = useState(false)
  const [missing, setMissing] = useState(false)
  const [waiting, setWaiting] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  // A new error ends the wait (success unmounts this screen); never spin forever.
  useEffect(() => {
    if (error) setWaiting(false)
    if (isTokenRejected(error)) inputRef.current?.focus()
  }, [error])
  useEffect(() => {
    if (!waiting) return
    const timer = window.setTimeout(() => setWaiting(false), 15_000)
    return () => window.clearTimeout(timer)
  }, [waiting])
  const rejected = isTokenRejected(error)
  const failure = missing ? { text: '请先输入访问令牌。', detail: undefined } : error ? describeFailure(error, '暂时无法解锁，请稍后再试。') : null
  return <main className="unlock-shell">
    <form className="unlock-card" noValidate onSubmit={event => {
      if (!tokenDraft.trim()) { event.preventDefault(); setMissing(true); inputRef.current?.focus(); return }
      setMissing(false); setWaiting(true); onSubmit(event)
    }}>
      <div className="brand-mark" aria-hidden="true">C</div>
      <h1>解锁 Conveyor 控制台</h1>
      <p className="unlock-lead">输入访问令牌即可使用，默认只在本标签页有效。</p>
      <label className="unlock-label" htmlFor="unlock-token">访问令牌</label>
      <div className={`token-field ${missing || rejected ? 'has-error' : ''}`}>
        <input id="unlock-token" ref={inputRef} type={showToken ? 'text' : 'password'} autoFocus autoComplete="current-password"
          autoCapitalize="off" autoCorrect="off" spellCheck={false} placeholder="输入访问令牌"
          value={tokenDraft} onChange={event => { setMissing(false); onTokenDraftChange(event.target.value) }}
          aria-invalid={missing || rejected} aria-describedby={failure ? 'unlock-error' : undefined} />
        <button type="button" className="token-toggle" aria-controls="unlock-token" aria-label={showToken ? '隐藏令牌' : '显示令牌'}
          onClick={() => { setShowToken(value => !value); inputRef.current?.focus() }}>{showToken ? '隐藏' : '显示'}</button>
      </div>
      {failure && <div className="unlock-error" id="unlock-error" role="alert">
        <span>{failure.text}</span>
        {failure.detail && <small>{failure.detail}</small>}
        {!missing && !rejected && canRetry && <button type="button" disabled={waiting} onClick={() => { setWaiting(true); onRetry() }}>重试</button>}
      </div>}
      <label className="remember-token">
        <input type="checkbox" checked={rememberToken} onChange={event => onRememberTokenChange(event.target.checked)} />
        <span>在这台设备上记住<small>令牌会保存在这个浏览器里，直到你点“锁定”。只在自己的设备上勾选。</small></span>
      </label>
      <button className="primary" type="submit" disabled={waiting}>{waiting ? '正在解锁…' : '解锁'}</button>
    </form>
  </main>
}

function QueueSummary({ states }: { states?: Record<string, number> }) {
  const count = (state: string) => states?.[state] || 0
  const running = count('running')
  const queued = count('queued')
  const history = ['completed', 'failed', 'cancelled', 'interrupted'].reduce((total, state) => total + count(state), 0)
  const detail = history
    ? `历史任务 ${history} 个：已完成 ${count('completed')} · 失败 ${count('failed')} · 已取消 ${count('cancelled')} · 已中断 ${count('interrupted')}`
    : '还没有历史任务'
  return running || queued
    ? <div className="queue-summary is-busy" title={detail}>
      <span className="queue-dot" aria-hidden="true" />
      <span>运行中 <strong>{running}</strong></span><span className="queue-sep" aria-hidden="true">·</span><span>排队 <strong>{queued}</strong></span>
    </div>
    : <div className="queue-summary is-idle" title={detail}><span>队列空闲</span></div>
}

type ProviderField = 'provider_id' | 'provider_name' | 'base_url' | 'model' | 'wire_api' | 'reasoning_effort' | 'env_key' | 'api_key'
type ProviderDraft = Record<ProviderField, string>
const PROVIDER_FIELDS: ProviderField[] = ['provider_id', 'provider_name', 'base_url', 'model', 'wire_api', 'reasoning_effort', 'env_key', 'api_key']
const REASONING_LABELS: [string, string][] = [['none', '不推理'], ['minimal', '最低'], ['low', '低'], ['medium', '中'], ['high', '高'], ['xhigh', '最高']]
const PROVIDER_HINTS: Partial<Record<ProviderField, string>> = {
  provider_id: '给这家服务商起个英文小名，选了上面的预设会自动填好',
  base_url: '服务商给你的接口网址，远程服务必须用 https',
  wire_api: '不确定就选「通用聊天接口」，选了预设会自动填好',
  model: '点「获取模型列表」从服务商读取可用模型',
  reasoning_effort: '越高越仔细，也越慢',
  env_key: '密钥在服务器上的存放名称，选了预设会自动填好，一般不用改',
}

function providerDraft(config: ProviderConfig | null): ProviderDraft {
  return {
    provider_id: config?.provider_id || '', provider_name: config?.provider_name || '', base_url: config?.base_url || '',
    // Chat Completions is what most OpenAI-compatible providers speak; Responses is only chosen explicitly (see OpenAI preset).
    model: config?.model || '', wire_api: config?.wire_api || 'chat', reasoning_effort: config?.reasoning_effort || 'minimal',
    env_key: config?.env_key || 'OPENAI_API_KEY', api_key: '',
  }
}

function validateProvider(draft: ProviderDraft, keyConfigured: boolean): Partial<Record<ProviderField, string>> {
  const errors: Partial<Record<ProviderField, string>> = {}
  const providerId = draft.provider_id.trim().toLowerCase()
  if (!providerId) errors.provider_id = '请填写服务商代号'
  else if (!/^[a-z][a-z0-9_-]{0,39}$/.test(providerId)) errors.provider_id = '需以小写字母开头，只能用小写字母、数字、- 和 _'
  if (!draft.provider_name.trim()) errors.provider_name = '请填写显示名称'
  else if (draft.provider_name.trim().length > 80) errors.provider_name = '显示名称最多 80 个字'
  const address = draft.base_url.trim()
  let url: URL | null = null
  try { url = new URL(address) } catch { url = null }
  if (!address) errors.base_url = '请填写接口地址'
  else if (!url || !['http:', 'https:'].includes(url.protocol) || !url.host) errors.base_url = '请填写完整网址，例如 https://api.deepseek.com/v1'
  else if (url.username || url.password) errors.base_url = '网址里不能带用户名或密码'
  else if (url.protocol === 'http:' && !['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)) errors.base_url = '远程服务必须使用 https'
  if (!draft.model.trim()) errors.model = '请填写模型名称'
  else if (draft.model.trim().length > 120) errors.model = '模型名称最多 120 个字符'
  if (!/^[A-Z][A-Z0-9_]{1,63}$/.test(draft.env_key.trim().toUpperCase())) errors.env_key = '只能用大写字母、数字和下划线，并以字母开头'
  const key = draft.api_key.trim()
  if (key && (key.length < 8 || key.length > 4096)) errors.api_key = '密钥长度应为 8–4096 个字符'
  else if (!key && !keyConfigured) errors.api_key = '还没有配置密钥，请填写'
  return errors
}

// Name prefixes the server refuses for 密钥名称 (provider_config.py _RUNTIME_PREFIXES).
const RESERVED_ENV_PREFIXES = ['LD_', 'PYTHON', 'CONVEYOR_', 'CODEX_', 'GIT_', 'NODE_', 'PI_', 'SSL_', 'REQUESTS_', 'CURL_', 'HTTP', 'NO_PROXY', 'PATH', 'HOME', 'SHELL', 'TELEGRAM_', 'LARK_']

/** 服务商代号 for a custom provider: from 显示名称, else from the 接口地址 host. Always [a-z][a-z0-9-]{0,39}. */
function deriveProviderCode(name: string, address: string): string {
  const slug = (value: string) => value.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '')
  let code = slug(name)
  if (!code) {
    let host = ''
    try { host = new URL(address.trim()).hostname } catch {
      try { host = new URL(`https://${address.trim()}`).hostname } catch { host = '' }
    }
    code = slug(host.replace(/^www\./, ''))
  }
  if (!code) return 'custom'
  if (!/^[a-z]/.test(code)) code = `p-${code}`
  return code.slice(0, 40).replace(/-+$/, '')
}

/** 密钥名称 default: <CODE>_API_KEY, moved under PROVIDER_ when the plain name would hit a reserved prefix. */
function deriveEnvKey(code: string): string {
  const name = `${code.toUpperCase().replace(/[^A-Z0-9]/g, '_')}_API_KEY`
  return RESERVED_ENV_PREFIXES.some(prefix => name.startsWith(prefix)) ? `PROVIDER_${name}` : name
}

/** A custom provider can be saved without opening 高级: fill only the two hidden fields that are still empty. */
function withDerivedIdentity(draft: ProviderDraft): ProviderDraft {
  const providerId = draft.provider_id.trim() ? draft.provider_id : deriveProviderCode(draft.provider_name, draft.base_url)
  const envKey = draft.env_key.trim() ? draft.env_key : deriveEnvKey(providerId.trim().toLowerCase())
  return { ...draft, provider_id: providerId, env_key: envKey }
}

/** Server messages (English) → a Chinese sentence, attached to the field it is about. */
function describeProviderError(message: string): { field?: ProviderField; text: string; detail?: string } {
  const rules: [RegExp, ProviderField, string][] = [
    [/^provider_id/i, 'provider_id', '需以小写字母开头，只能用小写字母、数字、- 和 _'],
    [/^provider_name/i, 'provider_name', '显示名称需要 1–80 个字'],
    [/^model must/i, 'model', '模型名称需要 1–120 个字符'],
    [/must use https/i, 'base_url', '远程服务必须使用 https'],
    [/^base_url/i, 'base_url', '请填写完整网址，且不能带用户名或密码'],
    [/^wire_api/i, 'wire_api', '请重新选择接口类型'],
    [/reasoning effort/i, 'reasoning_effort', '不支持这个推理强度'],
    [/environment key name/i, 'env_key', '只能用大写字母、数字和下划线，并以字母开头'],
    [/^api_key is required/i, 'api_key', '这个密钥名称下还没有密钥，请填写 API 密钥'],
    [/^api_key must be/i, 'api_key', '密钥长度应为 8–4096 个字符，且不能换行'],
  ]
  const rule = rules.find(([pattern]) => pattern.test(message))
  return rule ? { field: rule[1], text: rule[2] } : describeFailure(message, '保存没有成功，请稍后再试。')
}

/** Owner-facing (Chinese only) reason a model-list fetch failed. The raw server text is shown only under 技术详情. */
function describeModelsFailure(raw: string, code?: string): string {
  if (code === 'api_key_required') return '获取模型列表失败：请先填写 API 密钥。'
  const upstream = Number(raw.match(/(?:HTTP |returned )(\d{3})/)?.[1] ?? raw.match(/\b(401|403|404)\b/)?.[1]) || 0
  if (upstream === 501 || upstream === 405) return '这个服务商不支持自动获取模型列表，请直接在「模型」里填写模型名称。'
  if (upstream === 401 || upstream === 403) return `获取模型列表失败：API 密钥不对或没有权限（${upstream}）。请检查后再试。`
  if (upstream === 404) return '获取模型列表失败：接口地址不对（404）。请检查后再试。'
  if (/超时|网络|连接|timeout|network|refused|ECONN/i.test(raw)) return '获取模型列表失败：连不上服务商。请检查网络和接口地址后再试。'
  if (/服务地址|base url|接口地址/i.test(raw)) return '获取模型列表失败：接口地址还没填好或格式不对。请检查后再试。'
  return `获取模型列表失败：服务商没有正常返回${upstream ? `（${upstream}）` : ''}。请检查接口地址和 API 密钥后再试。`
}

/** Fields that live under the collapsed 高级 section of 模型设置. */
const PROVIDER_ADVANCED_FIELDS: ProviderField[] = ['provider_id', 'env_key', 'reasoning_effort']
/** The two machine names (服务商代号 / 密钥名称) sit one level deeper, under 技术设置 (QA N26). */
const PROVIDER_TECH_FIELDS: ProviderField[] = ['provider_id', 'env_key']

const PROVIDER_PRESETS = [
  {
    id: 'deepseek',
    label: 'DeepSeek',
    provider_id: 'deepseek',
    provider_name: 'DeepSeek',
    base_url: 'https://api.deepseek.com/v1',
    wire_api: 'chat',
    env_key: 'DEEPSEEK_API_KEY',
  },
  {
    id: 'stepfun',
    label: 'StepFun',
    provider_id: 'stepfun',
    provider_name: 'StepFun',
    base_url: 'https://api.stepfun.com/v1',
    wire_api: 'chat',
    env_key: 'STEPFUN_API_KEY',
  },
  {
    id: 'moonshot',
    label: 'Moonshot',
    provider_id: 'moonshot',
    provider_name: 'Moonshot',
    base_url: 'https://api.moonshot.cn/v1',
    wire_api: 'chat',
    env_key: 'MOONSHOT_API_KEY',
  },
  {
    id: 'openai',
    label: 'OpenAI',
    provider_id: 'openai',
    provider_name: 'OpenAI',
    base_url: 'https://api.openai.com/v1',
    wire_api: 'responses',
    env_key: 'OPENAI_API_KEY',
  },
] as const

function ProviderSettings({ config, loadError, onClose, onRetry, onSave }: {
  config: ProviderConfig | null; loadError: string; onClose: () => void; onRetry: () => void
  onSave: (payload: Record<string, string>) => Promise<void>
}) {
  const [draft, setDraft] = useState<ProviderDraft>(() => providerDraft(config))
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [confirmDiscard, setConfirmDiscard] = useState(false)
  const [error, setError] = useState<{ text: string; detail?: string } | null>(null)
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<ProviderField, string>>>({})
  const [fetchingModels, setFetchingModels] = useState(false)
  const [fetchedModels, setFetchedModels] = useState<string[]>([])
  const [fetchModelsError, setFetchModelsError] = useState('')
  const [fetchModelsNote, setFetchModelsNote] = useState('')
  const [fetchModelsDetail, setFetchModelsDetail] = useState('')
  const [advancedOpen, setAdvancedOpen] = useState(false)
  // 技术设置 (服务商代号 / 密钥名称) is nested inside 高级; opening either opens both.
  const [techOpen, setTechOpen] = useState(false)
  const [selectedPreset, setSelectedPreset] = useState<string>('')
  const sheetRef = useRef<HTMLElement>(null)
  const discardCancelRef = useRef<HTMLButtonElement>(null)
  const controls = useRef<Partial<Record<ProviderField, HTMLInputElement | HTMLSelectElement | null>>>({})
  // The app polls /api/config/provider and hands back a new object each time. Adopt the server values only
  // while nothing here is edited, so a poll never wipes what the owner is typing or has just picked.
  const baselineRef = useRef<ProviderDraft | null>(null)
  // After a successful save the server's copy wins, even if it normalised a field differently.
  const adoptServerRef = useRef(false)
  useEffect(() => {
    if (!config) return
    const next = providerDraft(config)
    const previous = baselineRef.current
    baselineRef.current = next
    const adopt = adoptServerRef.current
    adoptServerRef.current = false
    setDraft(current => (adopt || previous === null || PROVIDER_FIELDS.every(field => current[field] === previous[field]) ? next : current))
  }, [config])
  const baseline = providerDraft(config)
  const dirty = Boolean(config) && PROVIDER_FIELDS.some(field => draft[field] !== baseline[field])

  const activePreset =
    selectedPreset === 'custom'
      ? 'custom'
      : PROVIDER_PRESETS.find(
          p =>
            draft.provider_id === p.provider_id &&
            draft.base_url === p.base_url &&
            draft.wire_api === p.wire_api
        )?.id || (selectedPreset || (draft.provider_id.trim() || draft.base_url.trim() ? 'custom' : ''))

  function applyPreset(preset: typeof PROVIDER_PRESETS[number]) {
    setSaved(false)
    setSelectedPreset(preset.id)
    // A model name (and the list fetched for it) belongs to one provider: switching provider drops both.
    const providerChanged = draft.provider_id.trim().toLowerCase() !== preset.provider_id
    setDraft(previous => ({
      ...previous,
      provider_id: preset.provider_id,
      provider_name: preset.provider_name,
      base_url: preset.base_url,
      wire_api: preset.wire_api,
      env_key: preset.env_key,
      ...(providerChanged ? { model: '' } : {}),
    }))
    setFieldErrors(previous => ({
      ...previous,
      provider_id: undefined,
      provider_name: undefined,
      base_url: undefined,
      wire_api: undefined,
      env_key: undefined,
      ...(providerChanged ? { model: undefined } : {}),
    }))
    if (providerChanged) {
      setFetchedModels([]); setFetchModelsNote(''); setFetchModelsError(''); setFetchModelsDetail('')
    }
  }

  /** 自定义 starts from a blank provider: nothing from the previous preset is carried over. */
  function chooseCustom() {
    if (activePreset === 'custom') return
    setSaved(false)
    setSelectedPreset('custom')
    setDraft(previous => ({
      ...previous,
      // A custom provider starts blank, on Chat Completions (the usual OpenAI-compatible interface).
      provider_id: '', provider_name: '', base_url: '', env_key: '', model: '', api_key: '', wire_api: 'chat', reasoning_effort: 'minimal',
    }))
    setFieldErrors({})
    setFetchedModels([]); setFetchModelsNote(''); setFetchModelsError(''); setFetchModelsDetail('')
  }

  async function fetchProviderModels() {
    const filled = withDerivedIdentity(draft)
    setFetchingModels(true)
    setFetchModelsError('')
    setFetchModelsNote('')
    setFetchModelsDetail('')
    try {
      const token = sessionStorage.getItem('conveyor-token') || storedToken()
      // Send what is on screen (not only what was saved): a first-time setup can fetch before saving.
      // An empty api_key tells the server to fall back to the key it already stores.
      const response = await fetch('/api/config/provider/models', {
        method: 'POST',
        headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
        body: JSON.stringify({
          provider_id: filled.provider_id.trim().toLowerCase(),
          base_url: filled.base_url.trim(),
          wire_api: filled.wire_api,
          env_key: filled.env_key.trim().toUpperCase(),
          api_key: filled.api_key.trim(),
        }),
      })
      if (!response.ok) {
        const data = await response.json().catch(() => null) as { error?: string; code?: string } | null
        const raw = data?.error || `HTTP ${response.status}`
        setFetchModelsError(describeModelsFailure(raw, data?.code))
        setFetchModelsDetail(raw)
        return
      }
      const data = await response.json() as { models?: { id: string }[] }
      if (data && Array.isArray(data.models)) {
        const ids = data.models.map(m => m.id).filter(Boolean)
        setFetchedModels(ids)
        if (ids.length === 0) setFetchModelsError('服务商返回的模型列表为空。')
        else setFetchModelsNote(`已获取 ${ids.length} 个模型，点一下「模型」输入框就能选择。`)
      } else {
        setFetchModelsError('获取模型列表失败：服务商返回的内容看不懂。请稍后再试。')
      }
    } catch {
      setFetchModelsError('获取模型列表失败：连不上服务器。请检查网络后再试。')
    } finally {
      setFetchingModels(false)
    }
  }

  useEffect(() => {
    if (confirmDiscard) discardCancelRef.current?.focus()
  }, [confirmDiscard])

  // Focus the sheet (not a field: no phone keyboard popping up); give focus back on close.
  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null
    sheetRef.current?.focus()
    return () => { if (opener?.isConnected) opener.focus() }
  }, [])
  const requestClose = useCallback(() => {
    if (confirmDiscard) {
      setConfirmDiscard(false)
      return
    }
    if (dirty && !saving) {
      setConfirmDiscard(true)
      return
    }
    onClose()
  }, [confirmDiscard, dirty, onClose, saving])
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.isComposing || event.keyCode === 229) return
      event.preventDefault()
      if (confirmDiscard) {
        setConfirmDiscard(false)
        return
      }
      if (dirty && !saving) {
        setConfirmDiscard(true)
        return
      }
      onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [confirmDiscard, dirty, onClose, saving])

  function update(name: ProviderField, value: string) {
    adoptServerRef.current = false
    setSaved(false)
    setDraft(previous => ({ ...previous, [name]: value }))
    // QA N27: the red border belongs to the message under the field. Typing in a field
    // drops both, so a form that has just been filled never still looks invalid.
    setFieldErrors(previous => (previous[name] ? { ...previous, [name]: undefined } : previous))
  }
  async function submit(event: FormEvent) {
    event.preventDefault()
    if (saving || !config) return
    const filled = withDerivedIdentity(draft)
    const errors = validateProvider(filled, config.api_key_configured)
    setFieldErrors(errors); setError(null); setSaved(false)
    if (filled.provider_id !== draft.provider_id || filled.env_key !== draft.env_key) {
      setDraft(previous => ({ ...previous, provider_id: filled.provider_id, env_key: filled.env_key }))
    }
    const first = PROVIDER_FIELDS.find(field => errors[field])
    if (first) {
      // A field inside the collapsed 高级 section must be opened before it can take focus;
      // 服务商代号 / 密钥名称 live one level deeper, inside 技术设置.
      if (PROVIDER_ADVANCED_FIELDS.includes(first)) {
        setAdvancedOpen(true)
        if (PROVIDER_TECH_FIELDS.includes(first)) setTechOpen(true)
      }
      window.setTimeout(() => controls.current[first]?.focus(), 0)
      return
    }
    setSaving(true)
    adoptServerRef.current = true
    try {
      await onSave({
        ...filled, provider_id: filled.provider_id.trim().toLowerCase(), provider_name: filled.provider_name.trim(),
        base_url: filled.base_url.trim(), model: filled.model.trim(), env_key: filled.env_key.trim().toUpperCase(), api_key: filled.api_key.trim(),
      })
      setSaved(true)
      setDraft(previous => ({ ...previous, api_key: '' }))
    } catch (reason) {
      adoptServerRef.current = false
      const described = describeProviderError(reason instanceof Error ? reason.message : '')
      if (described.field) {
        const field = described.field
        setFieldErrors({ [field]: described.text })
        if (PROVIDER_ADVANCED_FIELDS.includes(field)) {
          setAdvancedOpen(true)
          if (PROVIDER_TECH_FIELDS.includes(field)) setTechOpen(true)
        }
        window.setTimeout(() => controls.current[field]?.focus(), 0)
      }
      else setError({ text: described.text, detail: described.detail })
    } finally { setSaving(false) }
  }
  const hint = (name: ProviderField) => name === 'api_key' && config && !config.api_key_configured ? '还没有配置密钥，首次保存必须填写' : PROVIDER_HINTS[name]
  // QA N27: a field's red state follows its current value, not only the stored message, so a
  // field the owner has just filled can never keep a red border (a filled form looks valid).
  const stillInvalid = (name: ProviderField) =>
    Boolean(fieldErrors[name]) && Boolean(validateProvider(draft, Boolean(config?.api_key_configured))[name])
  const control = (name: ProviderField) => ({
    id: `provider-${name}`,
    ref: (element: HTMLInputElement | HTMLSelectElement | null) => { controls.current[name] = element },
    value: draft[name],
    'aria-invalid': stillInvalid(name),
    'aria-describedby': fieldErrors[name] || hint(name) ? `provider-${name}-note` : undefined,
  })
  const field = (name: ProviderField, label: string, input: React.ReactNode, wide = false) => {
    const message = stillInvalid(name) ? fieldErrors[name] : undefined
    return (
      <div className={`dialog-field ${wide ? 'wide' : ''} ${message ? 'has-error' : ''}`}>
        <label htmlFor={`provider-${name}`}>{label}</label>
        {input}
        {message
          ? <p className="field-error" id={`provider-${name}-note`}>{message}</p>
          : hint(name) ? <p className="field-hint" id={`provider-${name}-note`}>{hint(name)}</p> : null}
      </div>
    )
  }
  const text = { autoCapitalize: 'off', autoCorrect: 'off', spellCheck: false, autoComplete: 'off' } as const

  return <div className="settings-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) requestClose() }}>
    <section ref={sheetRef} tabIndex={-1} className="settings-sheet provider-settings" role="dialog" aria-modal="true" aria-labelledby="provider-settings-title" onKeyDown={keepTabInside}>
      <header>
        <div><h2 id="provider-settings-title">模型设置</h2><p>新任务会使用这里配置的模型服务。</p></div>
        <button type="button" className="close-button" onClick={requestClose} aria-label="关闭">×</button>
      </header>
      {!config ? (loadError
        ? <div className="settings-load-error" role="alert"><strong>读取模型设置失败</strong><span>{describeFailure(loadError, '请稍后再试。').text}</span><button type="button" onClick={onRetry}>重试</button></div>
        : <div className="settings-loading" role="status">加载中…</div>)
      : <form onSubmit={event => void submit(event)} noValidate>
        <div className="sheet-body">
          <div className="provider-presets-section" role="group" aria-label="服务商预设">
            <span className="provider-presets-label">服务商预设：</span>
            <div className="provider-presets-chips">
              {PROVIDER_PRESETS.map(preset => (
                <button
                  key={preset.id}
                  type="button"
                  className={`provider-preset-chip ${activePreset === preset.id ? 'is-active' : ''}`}
                  onClick={() => applyPreset(preset)}
                >
                  {preset.label}
                </button>
              ))}
              <button
                type="button"
                className={`provider-preset-chip ${activePreset === 'custom' ? 'is-active' : ''}`}
                onClick={chooseCustom}
              >
                自定义
              </button>
            </div>
          </div>
          <div className="form-grid">
            {field('provider_name', '显示名称', <input {...control('provider_name')} autoComplete="off" placeholder="我的模型" onChange={event => update('provider_name', event.target.value)} />)}
            {field('base_url', '接口地址', <input {...control('base_url')} {...text} type="url" inputMode="url" className="mono-input" placeholder="https://你的服务地址/v1" onChange={event => update('base_url', event.target.value)} />, true)}
            {field('model', '模型', (
              <div className="provider-model-wrapper">
                <div className="provider-model-row">
                  <input
                    {...control('model')}
                    {...text}
                    className="mono-input"
                    placeholder="模型名称，例如 gpt-4o-mini"
                    list={fetchedModels.length > 0 ? 'provider-models-datalist' : undefined}
                    onChange={event => update('model', event.target.value)}
                  />
                  <button
                    type="button"
                    className="guard-btn guard-btn-small provider-fetch-models-btn"
                    disabled={fetchingModels}
                    onClick={() => { void fetchProviderModels() }}
                  >
                    {fetchingModels ? '获取中…' : '获取模型列表'}
                  </button>
                </div>
                {fetchedModels.length > 0 && (
                  <datalist id="provider-models-datalist">
                    {fetchedModels.map(id => (
                      <option key={id} value={id} />
                    ))}
                  </datalist>
                )}
                {fetchModelsNote && !fetchModelsError && (
                  <p className="field-hint provider-fetch-note" role="status">{fetchModelsNote}</p>
                )}
                {fetchModelsError && (
                  <p className="field-error provider-fetch-error" role="alert">{fetchModelsError}</p>
                )}
                {fetchModelsError && fetchModelsDetail && (
                  <details className="guard-tech-details provider-fetch-tech">
                    <summary>技术详情</summary>
                    <span className="guard-tech">{fetchModelsDetail}</span>
                  </details>
                )}
              </div>
            ))}
            {field('wire_api', '接口类型', <select {...control('wire_api')} onChange={event => update('wire_api', event.target.value)}><option value="chat">通用聊天接口</option><option value="responses">新版应答接口</option></select>)}
            {field('api_key', 'API 密钥', <input {...control('api_key')} {...text} type="password" placeholder={config.api_key_configured ? `已配置 ${config.api_key_hint}，留空则保持不变` : '粘贴 API 密钥'} onChange={event => update('api_key', event.target.value)} />, true)}
            <details className="provider-advanced wide" open={advancedOpen} onToggle={event => setAdvancedOpen(event.currentTarget.open)}>
              <summary>高级</summary>
              <div className="form-grid provider-advanced-grid">
                {/* 服务商代号 and 密钥名称 are machine names the owner never types: they sit one
                    level deeper, under 技术设置, with one line saying they fill themselves in (QA N26). */}
                <details className="provider-tech wide" open={techOpen} onToggle={event => setTechOpen(event.currentTarget.open)}>
                  <summary>技术设置</summary>
                  <p className="provider-tech-note">这两项按上面的「显示名称」和「接口地址」自动填好，一般不用改。</p>
                  <div className="form-grid provider-tech-grid">
                    {field('provider_id', '服务商代号', <input {...control('provider_id')} {...text} className="mono-input" placeholder="deepseek" onChange={event => update('provider_id', event.target.value)} />)}
                    {field('env_key', '密钥名称', <input {...control('env_key')} {...text} className="mono-input" placeholder="OPENAI_API_KEY" onChange={event => update('env_key', event.target.value.toUpperCase())} />)}
                  </div>
                </details>
                {field('reasoning_effort', '推理强度', <select {...control('reasoning_effort')} onChange={event => update('reasoning_effort', event.target.value)}>{REASONING_LABELS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select>)}
              </div>
            </details>
          </div>
          <div className="config-note">
            <strong>密钥只保存在服务器上</strong>
            <span>浏览器拿不到完整密钥。保存后，从下一个新任务开始生效。</span>
            <small>设置保存在服务器上。</small>
          </div>
        </div>
        {confirmDiscard ? (
          <footer className="settings-discard-confirm" role="group" aria-label="确认放弃修改">
            <p>
              <strong>放弃还没保存的修改？</strong>
              <span>未保存的配置项变动将会丢失。</span>
            </p>
            <div className="settings-confirm-buttons">
              <button type="button" ref={discardCancelRef} onClick={() => setConfirmDiscard(false)}>继续编辑</button>
              <button type="button" className="danger-solid" onClick={onClose}>放弃修改</button>
            </div>
          </footer>
        ) : (
          <footer>
            {error && <div className="error-banner dialog-error" role="alert"><span>{error.text}</span>{error.detail && <small>{error.detail}</small>}</div>}
            <span className={`save-status ${saved ? 'visible' : ''}`} role="status">{saved ? '✓ 已保存，新任务会使用这个配置。' : ''}</span>
            <button type="button" onClick={requestClose}>{saved && !dirty ? '完成' : dirty ? '取消' : '关闭'}</button>
            <button type="submit" className="primary" disabled={saving}>{saving ? '正在保存…' : '保存'}</button>
          </footer>
        )}
      </form>}
    </section>
  </div>
}

function StatusBadge({ state }: { state: string }) { return <span className={`status-badge ${state}`}><i />{stateLabel(state)}</span> }
function Empty({ text }: { text: string }) { return <div className="empty">{text}</div> }
function KeyValue({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) { return <div className="key-value"><span>{label}</span><strong className={mono ? 'mono' : ''} title={mono ? value : undefined}>{value}</strong></div> }
function ContextSection({
  title,
  children,
  collapsible = false,
  storageKey,
  defaultCollapsed = false,
  resetKey,
  autoExpandKey,
  className = '',
  meta,
  hideTitle = false,
}: {
  title: string
  className?: string
  /** Short summary beside the title (e.g. "3 个文件"); stays visible when collapsed. */
  meta?: string
  children: React.ReactNode
  collapsible?: boolean
  storageKey?: string
  defaultCollapsed?: boolean
  resetKey?: string
  autoExpandKey?: string | null
  /** The selected tab already names this section (资料 / 电脑): label it for screen readers only. */
  hideTitle?: boolean
}) {
  const [collapsed, setCollapsed] = useState(() => {
    if (!collapsible) return false
    if (!storageKey) return defaultCollapsed
    try {
      const stored = localStorage.getItem(storageKey)
      return stored !== null ? stored === 'true' : defaultCollapsed
    } catch {
      return defaultCollapsed
    }
  })
  const lastResetRef = useRef(resetKey)
  const seenAutoExpandRef = useRef<Set<string>>(new Set())

  if (collapsible) {
    if (resetKey !== undefined && lastResetRef.current !== resetKey) {
      lastResetRef.current = resetKey
      if (autoExpandKey) {
        seenAutoExpandRef.current.add(autoExpandKey)
        if (collapsed) setCollapsed(false)
      } else if (!storageKey && defaultCollapsed && !collapsed) {
        setCollapsed(true)
      }
    } else if (autoExpandKey && !seenAutoExpandRef.current.has(autoExpandKey)) {
      seenAutoExpandRef.current.add(autoExpandKey)
      if (collapsed) setCollapsed(false)
    }
  }

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

  if (hideTitle && !collapsible) return <section className={`context-section ${className}`} aria-label={title}>{children}</section>
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
            <span>{title}</span>
            {meta && <span className="context-section-meta">{meta}</span>}
            <span className="context-collapse-icon" aria-hidden="true">
              {collapsed ? '▸' : '▾'}
            </span>
          </button>
        ) : (
          <>{title}{meta && <span className="context-section-meta">{meta}</span>}</>
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
      <button ref={triggerRef} type="button" className="screen-image-trigger" onClick={() => setExpanded(true)} aria-label="放大查看主机截图">
        <img src={url} alt="最近一次截取的主机屏幕" />
        <span>放大</span>
      </button>
      <figcaption><span>{artifact.width && artifact.height ? `${artifact.width} × ${artifact.height} · ` : ''}{formatTime(artifact.created_at)}</span><span>{artifact.node_id || 'Mac'}</span></figcaption>
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
          <div><p className="eyebrow">Mac · 只读</p><h2 id="screen-viewer-title">主机截图</h2><p>{artifact.width && artifact.height ? `${artifact.width} × ${artifact.height} · ` : ''}截取于 {formatTime(artifact.created_at)}</p></div>
          <button ref={closeRef} type="button" className="close-button" onClick={() => setExpanded(false)} aria-label="关闭截图">×</button>
        </header>
        <div className="screen-viewer-image-frame"><img src={url} alt="放大的主机截图" /></div>
        <footer className="screen-viewer-footer"><span>{artifact.node_id || 'Mac'} · 缩略图</span><span>原图只保存在 Mac 上，不会持续传输，也不能远程操作。</span></footer>
      </section>
    </div>}
  </> : null
}
