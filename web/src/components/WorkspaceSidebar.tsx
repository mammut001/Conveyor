import { useMemo, useState } from 'react'
import type { Job, Session, SystemStatus } from '../types'

function formatTime(value?: string) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.valueOf())) return value
  const now = Date.now()
  const delta = Math.max(0, now - date.valueOf())
  const minutes = Math.floor(delta / 60_000)
  if (minutes < 1) return 'now'
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h`
  const days = Math.floor(hours / 24)
  if (days < 14) return `${days}d`
  return date.toLocaleDateString([], { month: 'short', day: 'numeric' })
}

function sessionTitle(session: Session) {
  return session.title || session.latest_job?.prompt_preview || 'Untitled session'
}

function sessionPreview(session: Session) {
  const preview = session.latest_job?.prompt_preview?.trim()
  if (preview && preview !== session.title) return preview
  const count = session.message_count ?? 0
  return `${count} message${count === 1 ? '' : 's'} · ${session.job_count} job${session.job_count === 1 ? '' : 's'}`
}

function statusLabel(session: Session) {
  const state = session.latest_job?.state
  if (state === 'running') return 'Working'
  if (state === 'queued') return 'Queued'
  if (state === 'failed' || state === 'interrupted') return 'Needs attention'
  return ''
}

const ATTENTION_COLLAPSE_THRESHOLD = 6

function sessionGroup(session: Session) {
  const state = session.latest_job?.state
  if (state === 'running' || state === 'queued') return 'active' as const
  if (state === 'failed' || state === 'interrupted') return 'attention' as const
  return 'recent' as const
}

export function WorkspaceSidebar({
  sessions,
  jobs,
  selectedSessionId,
  system,
  collapsed,
  onToggleCollapsed,
  onNewSession,
  onSelectSession,
}: {
  sessions: Session[]
  jobs: Job[]
  selectedSessionId: string
  system: SystemStatus | null
  collapsed: boolean
  onToggleCollapsed: () => void
  onNewSession: () => void
  onSelectSession: (session: Session) => void
}) {
  const runningCount = jobs.filter(job => job.state === 'running').length
  const queuedCount = jobs.filter(job => job.state === 'queued').length
  const [query, setQuery] = useState('')
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase()
    if (!needle) return sessions
    return sessions.filter(session => [session.title, session.latest_job?.prompt_preview, session.channel]
      .some(value => value?.toLowerCase().includes(needle)))
  }, [query, sessions])
  const grouped = useMemo(() => {
    const groups = {
      active: [] as Session[],
      attention: [] as Session[],
      recent: [] as Session[],
    }
    filtered.forEach(session => groups[sessionGroup(session)].push(session))
    return groups
  }, [filtered])
  const [needsAttentionExpanded, setNeedsAttentionExpanded] = useState(false)
  const selectedNeedsAttention = grouped.attention.some(session => session.id === selectedSessionId)
  const needsAttentionCollapsed = grouped.attention.length > ATTENTION_COLLAPSE_THRESHOLD
    && !needsAttentionExpanded
    && !selectedNeedsAttention
  const railSessions = useMemo(() => {
    const visible = sessions.slice(0, 8)
    const selected = sessions.find(session => session.id === selectedSessionId)
    if (!selected || visible.some(session => session.id === selected.id)) return visible
    return [selected, ...visible.slice(0, 7)]
  }, [selectedSessionId, sessions])

  if (collapsed) return <aside className="v3-sidebar collapsed" aria-label="Conveyor sessions">
    <button className="v3-rail-button brand" onClick={onToggleCollapsed} title="Expand sidebar" aria-label="Expand sidebar">C</button>
    <button className="v3-rail-button" onClick={onNewSession} title="New chat" aria-label="New chat">＋</button>
    <div className="v3-rail-sessions">
      {railSessions.map(session => <button
        key={session.id}
        className={`v3-rail-dot ${session.latest_job?.state || ''} ${session.id === selectedSessionId ? 'active' : ''}`}
        aria-label={sessionTitle(session)}
        title={sessionTitle(session)}
        onClick={() => onSelectSession(session)}
      />)}
    </div>
  </aside>

  const renderGroup = (
    label: string,
    items: Session[],
    options?: { collapsible?: boolean; collapsed?: boolean; listId?: string; onToggle?: () => void },
  ) => items.length ? <section className={`v3-session-group ${options?.collapsible ? 'is-collapsible' : ''}`}>
    {options?.collapsible ? <button
      className="v3-section-toggle"
      type="button"
      aria-expanded={!options.collapsed}
      aria-controls={options.listId}
      aria-label={`${options.collapsed ? 'Expand' : 'Collapse'} ${label} (${items.length})`}
      onClick={options.onToggle}
    >
      <span className="v3-section-label"><span>{label}</span><span>{items.length}</span></span>
      <span className="v3-section-toggle-icon" aria-hidden="true">{options.collapsed ? '+' : '−'}</span>
    </button> : <div className="v3-section-label"><span>{label}</span><span>{items.length}</span></div>}
    <div id={options?.listId} className="v3-session-list" hidden={options?.collapsed}>
      {items.map(session => {
        const state = session.latest_job?.state || 'idle'
        const activity = statusLabel(session)
        const selected = session.id === selectedSessionId
        return <button
          key={session.id}
          className={`v3-session-row ${selected ? 'active' : ''}`}
          aria-current={selected ? 'true' : undefined}
          title={sessionTitle(session)}
          onClick={() => onSelectSession(session)}
        >
          <span className={`v3-session-status ${state}`} />
          <span className="v3-session-copy">
            <span className="v3-session-title">{sessionTitle(session)}</span>
            <span className="v3-session-preview">{activity || sessionPreview(session)}</span>
          </span>
          <span className="v3-session-time">{formatTime(session.last_activity)}</span>
        </button>
      })}
    </div>
  </section> : null

  return <aside className="v3-sidebar" aria-label="Conveyor sessions">
    <div className="v3-sidebar-header">
      <div className="v3-brand-lockup"><span className="v3-logo">C</span><span><strong>Conveyor</strong><small>Agent workspace</small></span></div>
      <button className="v3-icon-button" onClick={onToggleCollapsed} aria-label="Collapse sidebar">‹</button>
    </div>
    <button className="v3-new-chat" onClick={onNewSession}><span>＋</span> New chat</button>
    <label className="v3-search"><span>⌕</span><input value={query} onChange={event => setQuery(event.target.value)} placeholder="Search sessions" aria-label="Search sessions" /></label>
    <div className="v3-sidebar-scroll">
      {renderGroup('Active', grouped.active)}
      {renderGroup('Needs attention', grouped.attention, {
        collapsible: true,
        collapsed: needsAttentionCollapsed,
        listId: 'v3-needs-attention-list',
        onToggle: () => setNeedsAttentionExpanded(expanded => !expanded),
      })}
      {renderGroup('Recent', grouped.recent)}
      {!filtered.length && <div className="v3-sidebar-empty">{query ? `No sessions match “${query}”.` : 'No conversations yet.'}</div>}
    </div>
    <div className="v3-sidebar-footer">
      <div><span className="v3-live-dot" />{system?.queue.paused ? 'Queue paused' : 'Online'}</div>
      <div>{runningCount} working · {queuedCount} queued</div>
    </div>
  </aside>
}
