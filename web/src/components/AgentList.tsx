import { FormEvent, useEffect, useRef, useState } from 'react'

export type Agent = {
  id: string
  name: string
  color: string
  instructions: string
  workspace_path: string
  workspace_status?: '' | 'ok' | 'missing' | 'not_git'
  session_id: string
  is_default: boolean
  status: 'idle' | 'working' | 'waiting'
  last_message: string
  last_message_role?: string | null
  last_activity?: string | null
  message_count: number
  sessions?: WorkerSession[]
}

export type WorkerSession = {
  id: string
  title: string
  kind: string
  source_chat_id: string
}

export const AGENT_COLORS = ['#2f7df6', '#f59e0b', '#f97316', '#8b5cf6', '#10b981', '#ec4899', '#a16207', '#64748b']

/** Last path segment: what the agent is "about", shown as a tag beside its name. */
export function agentTag(agent: Pick<Agent, 'workspace_path'>): string {
  return agent.workspace_path.replace(/\/+$/, '').split('/').pop() || ''
}

export function AgentAvatar({ agent, size = 44 }: { agent: Pick<Agent, 'name' | 'color'>; size?: number }) {
  return (
    <span className="agent-avatar" style={{ width: size, height: size, background: agent.color, fontSize: Math.round(size * 0.42) }} aria-hidden="true">
      {(agent.name.trim()[0] || '?').toUpperCase()}
    </span>
  )
}

function preview(agent: Agent): string {
  if (agent.status === 'waiting') return 'Waiting for you'
  if (agent.status === 'working') return 'Working…'
  if (agent.last_message) return (agent.last_message_role === 'user' ? 'You: ' : '') + agent.last_message
  return agent.instructions || 'No messages yet'
}

export function WorkerSessionPicker({ sessions, selectedId, onSelect, onCreate, creating }: {
  sessions: WorkerSession[]
  selectedId: string
  onSelect: (sessionId: string) => void
  onCreate: () => void
  creating?: boolean
}) {
  if (!sessions.length) return null
  return (
    <div className="worker-session-picker">
      <select aria-label="Worker session" value={selectedId} onChange={event => onSelect(event.target.value)} disabled={creating}>
        {sessions.map(session => (
          <option key={session.id} value={session.id}>{session.title || session.id}</option>
        ))}
      </select>
      <button type="button" onClick={onCreate} disabled={creating}>{creating ? '创建中…' : '新建会话'}</button>
    </div>
  )
}

export function AgentList({ agents, selectedId, onSelect, onNew, onEdit, workerSessions, selectedWorkerSessionId, onSelectWorkerSession, onCreateWorkerSession, creatingWorkerSession }: {
  agents: Agent[]
  selectedId: string
  onSelect: (agent: Agent) => void
  onNew: () => void
  onEdit: (agent: Agent) => void
  workerSessions?: WorkerSession[]
  selectedWorkerSessionId?: string
  onSelectWorkerSession?: (sessionId: string) => void
  onCreateWorkerSession?: () => void
  creatingWorkerSession?: boolean
}) {
  return (
    <>
      <div className="panel-heading">
        <div><p className="eyebrow">AGENTS</p><h2>Conversations</h2></div>
        <button className="icon-button" onClick={onNew} aria-label="New agent">＋</button>
      </div>
      <div className="session-list agent-list">
        {agents.map(agent => (
          <div key={agent.id} className="agent-row">
            <button
              type="button"
              className={`agent-item ${agent.id === selectedId ? 'active' : ''}`}
              aria-pressed={agent.id === selectedId}
              onClick={() => onSelect(agent)}
            >
              <AgentAvatar agent={agent} />
              <span className="agent-item-text">
                <span className="agent-item-title">
                  <strong>{agent.name}</strong>
                  {agentTag(agent) && <span className="agent-tag">{agentTag(agent)}</span>}
                </span>
                <small className={agent.status === 'waiting' ? 'waiting' : ''}>{preview(agent)}</small>
              </span>
              {agent.status !== 'idle' && <span className={`agent-status-dot ${agent.status}`} aria-label={agent.status === 'waiting' ? 'Waiting for you' : 'Working'} />}
            </button>
            <button type="button" className="agent-edit-btn" title="Edit agent" aria-label={`Edit ${agent.name}`} onClick={() => onEdit(agent)}>⋯</button>
            {agent.id === selectedId && workerSessions && onSelectWorkerSession && onCreateWorkerSession && (
              <WorkerSessionPicker
                sessions={workerSessions}
                selectedId={selectedWorkerSessionId || workerSessions[0]?.id || ''}
                onSelect={onSelectWorkerSession}
                onCreate={onCreateWorkerSession}
                creating={creatingWorkerSession}
              />
            )}
          </div>
        ))}
      </div>
    </>
  )
}

export type AgentDraft = { name: string; instructions: string; workspace_path: string; color: string }

export function AgentDialog({ agent, onClose, onSave, onArchive }: {
  agent?: Agent
  onClose: () => void
  onSave: (draft: AgentDraft) => Promise<void>
  onArchive?: () => Promise<void>
}) {
  const [draft, setDraft] = useState<AgentDraft>(() => ({
    name: agent?.name || '',
    instructions: agent?.instructions || '',
    workspace_path: agent?.workspace_path || '',
    color: agent?.color || AGENT_COLORS[Math.floor(Math.random() * AGENT_COLORS.length)],
  }))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const nameRef = useRef<HTMLInputElement>(null)

  useEffect(() => { nameRef.current?.focus() }, [])
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const run = async (action: () => Promise<void>) => {
    setBusy(true); setError('')
    try { await action(); onClose() }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Could not save the agent') }
    finally { setBusy(false) }
  }
  const submit = (event: FormEvent) => { event.preventDefault(); void run(() => onSave({ ...draft, name: draft.name.trim() })) }

  return (
    <div className="settings-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) onClose() }}>
      <section className="settings-sheet agent-dialog" role="dialog" aria-modal="true" aria-label={agent ? `Edit ${agent.name}` : 'New agent'}>
        <header className="agent-dialog-header">
          <AgentAvatar agent={{ name: draft.name || '?', color: draft.color }} size={52} />
          <div><p className="eyebrow">{agent ? 'EDIT AGENT' : 'NEW AGENT'}</p><h2>{draft.name.trim() || 'Untitled agent'}</h2></div>
          <button type="button" className="close-button" onClick={onClose} aria-label="Close">×</button>
        </header>
        <form onSubmit={submit}>
          <label>Name
            <input ref={nameRef} value={draft.name} maxLength={60} required onChange={event => setDraft({ ...draft, name: event.target.value })} placeholder="Astra" />
          </label>
          <label>What is this agent for?
            <textarea value={draft.instructions} maxLength={4000} rows={6} onChange={event => setDraft({ ...draft, instructions: event.target.value })}
              placeholder="Its role and standing instructions. It sees these in every reply and every task it runs in this conversation." />
          </label>
          <label>Project folder on the host <small>optional</small>
            <input value={draft.workspace_path} maxLength={512} onChange={event => setDraft({ ...draft, workspace_path: event.target.value })} placeholder="/srv/my-project" spellCheck={false} autoCapitalize="off" />
          </label>
          <div className="agent-colors" role="radiogroup" aria-label="Color">
            {AGENT_COLORS.map(color => (
              <button key={color} type="button" role="radio" aria-checked={draft.color === color} aria-label={color}
                className={draft.color === color ? 'selected' : ''} style={{ background: color }} onClick={() => setDraft({ ...draft, color })} />
            ))}
          </div>
          {error && <div className="error-banner" role="alert">{error}</div>}
          <footer className="agent-dialog-actions">
            {agent && !agent.is_default && onArchive && (
              <button type="button" className="danger" disabled={busy} onClick={() => {
                if (window.confirm(`Remove ${agent.name}? Its conversation is kept but hidden.`)) void run(onArchive)
              }}>Remove</button>
            )}
            <span />
            <button type="button" disabled={busy} onClick={onClose}>Cancel</button>
            <button type="submit" className="primary" disabled={busy || !draft.name.trim()}>{busy ? 'Saving…' : agent ? 'Save' : 'Create agent'}</button>
          </footer>
        </form>
      </section>
    </div>
  )
}
