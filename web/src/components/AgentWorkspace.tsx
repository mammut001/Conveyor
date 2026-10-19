import { useCallback, useEffect, useState } from 'react'
import { ChatPanel, type ChatPanelProps } from './ChatPanel'
import { MemoryPanel } from './MemoryPanel'
import { ConversationPicker } from './ConversationPicker'
import type { Agent } from './AgentList'
import './agentScreens.css'

type Features = { enabled: boolean; inbox: boolean; tasks: boolean }
type Message = {
  id: string; from_agent: string; to_agent: string; kind: string
  body: string; status: string; created_at: number; untrusted: boolean
}
type Task = {
  id: string; from_agent: string; to_agent: string; status: string
  attempts: number; created_at: number; deadline: number
}
type Tab = 'chat' | 'inbox' | 'tasks' | 'memory'
type Props = ChatPanelProps & { agent: Agent }

export function AgentWorkspace({ agent, token, ...chatProps }: Props) {
  const [features, setFeatures] = useState<Features | null>(null)
  const [tab, setTab] = useState<Tab>('chat')
  const [conversation, setConversation] = useState(() => {
    try { return sessionStorage.getItem(`conveyor-agent-conversation:${agent.id}`) || agent.session_id }
    catch { return agent.session_id }
  })
  const [conversationReady, setConversationReady] = useState(false)
  const [chatBusy, setChatBusy] = useState(false)
  const chooseConversation = useCallback((session: string) => {
    setConversation(session)
    try { sessionStorage.setItem(`conveyor-agent-conversation:${agent.id}`, session) } catch { /* optional persistence */ }
  }, [agent.id])
  const conversationLoaded = useCallback(() => setConversationReady(true), [])

  useEffect(() => {
    let alive = true
    setFeatures(null)
    setTab('chat')
    fetch(`/api/agent-screens/config?agent_id=${encodeURIComponent(agent.id)}`, {
      headers: { Authorization: `Bearer ${token}` },
    }).then(async response => response.ok ? response.json() as Promise<Features> : null)
      .then(data => { if (alive) setFeatures(data) })
      .catch(() => { if (alive) setFeatures(null) })
    return () => { alive = false }
  }, [agent.id, token])

  const chat = conversationReady && <ChatPanel key={`${agent.id}:${conversation}`} {...chatProps} token={token}
    agentSessionId={conversation} agentName={agent.name} onBusyChange={setChatBusy}
    active={chatProps.active !== false && (!features?.enabled || tab === 'chat')} />

  return (
    <div className="agent-screen" aria-label={`${agent.name} workspace`}>
      {features?.enabled && <nav className="agent-screen-tabs" aria-label="Agent sections">
        {(['chat', ...(features.inbox ? ['inbox'] : []),
          ...(features.tasks ? ['tasks'] : []), 'memory'] as Tab[]).map(item => (
          <button type="button" key={item}
            aria-current={tab === item ? 'page' : undefined}
            className={tab === item ? 'active' : ''}
            onClick={() => setTab(item)}>{item[0].toUpperCase() + item.slice(1)}</button>
        ))}
      </nav>}
      <div hidden={tab !== 'chat'}>
        <ConversationPicker key={agent.id} token={token} agentId={agent.id}
          defaultSession={agent.session_id} selected={conversation} busy={chatBusy}
          onSelect={chooseConversation} onReady={conversationLoaded} />
      </div>
      <div style={{ display: !features?.enabled || tab === 'chat' ? 'flex' : 'none', flexDirection: 'column', flex: 1, minWidth: 0, minHeight: 0 }}>
        {chat}
      </div>
      {features?.enabled && tab !== 'chat' && (tab === 'memory'
        ? <MemoryPanel key={agent.id} token={token}
            agentId={agent.is_default ? undefined : agent.id} agentName={agent.name} />
        : <AgentMailboxTab key={`${agent.id}:${tab}`} token={token}
            agentId={agent.id} tab={tab as 'inbox' | 'tasks'} />)}
    </div>
  )
}

function AgentMailboxTab({ token, agentId, tab }: {
  token: string; agentId: string; tab: 'inbox' | 'tasks'
}) {
  const [items, setItems] = useState<(Message | Task)[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [cancelling, setCancelling] = useState<string | null>(null)

  const reload = useCallback(async (signal?: AbortSignal) => {
    try {
      const query = new URLSearchParams({ agent_id: agentId, view: tab })
      const response = await fetch(`/api/agent-screens?${query}`, {
        headers: { Authorization: `Bearer ${token}` }, signal,
      })
      if (!response.ok) throw new Error(`Could not read ${tab} (${response.status})`)
      const data = await response.json() as { items: (Message | Task)[] }
      if (!signal?.aborted) { setItems(data.items); setError('') }
    } catch (reason) {
      if (!signal?.aborted) setError(String(reason))
    } finally {
      if (!signal?.aborted) setLoading(false)
    }
  }, [token, agentId, tab])

  useEffect(() => {
    const controller = new AbortController()
    void reload(controller.signal)
    return () => controller.abort()
  }, [reload])

  const cancel = async (taskId: string) => {
    setCancelling(taskId)
    try {
      const response = await fetch('/api/agent-screens/cancel', {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/json',
          'X-Requested-With': 'XMLHttpRequest',
        },
        body: JSON.stringify({ agent_id: agentId, task_id: taskId }),
      })
      if (!response.ok) throw new Error(`Cancel failed (${response.status})`)
      await reload()
    } catch (reason) {
      setError(String(reason))
    } finally { setCancelling(null) }
  }

  return <section className="agent-screen-content">
    <header className="agent-screen-content-header">
      <h3>{tab === 'inbox' ? 'Agent Inbox' : 'Delegated Tasks'}</h3>
      <button type="button" onClick={() => void reload()}>Refresh</button>
    </header>
    {loading && <p>Loading…</p>}
    {error && <p role="alert">{error}</p>}
    {!loading && !items.length && <p>No {tab === 'inbox' ? 'messages' : 'delegated tasks'} yet.</p>}
    {tab === 'inbox' ? (items as Message[]).map(item =>
      <article className="agent-screen-item" key={item.id}>
        <p className="agent-screen-meta">{item.from_agent} → {item.to_agent} · {item.kind} · {item.status}</p>
        <small>Untrusted message · No instruction or approval authority</small>
        {/* Text child only. NEVER innerHTML, markdown or arbitrary HTML. */}
        <pre className="agent-screen-message">{item.body}</pre>
      </article>
    ) : (items as Task[]).map(item =>
      <article className="agent-screen-item" key={item.id}>
        <p className="agent-screen-meta">{item.from_agent} → {item.to_agent}</p>
        <p>Task {item.id.slice(0, 12)} · <strong>{item.status}</strong> · attempts {item.attempts}</p>
        {['queued', 'leased', 'failed'].includes(item.status) &&
          <button type="button" disabled={cancelling === item.id}
            onClick={() => void cancel(item.id)}>
            {cancelling === item.id ? 'Cancelling…' : 'Cancel task'}
          </button>}
      </article>
    )}
  </section>
}
