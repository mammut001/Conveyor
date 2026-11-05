import { useCallback, useEffect, useId, useRef, useState, type KeyboardEvent } from 'react'
import { ChatPanel, type ChatPanelProps } from './ChatPanel'
import { ConversationPicker } from './ConversationPicker'
import type { Agent } from './AgentList'
import { ChatRequestError, describeChatError, formatFullTime, formatRelativeTime } from './chatFormat'
import { useMediaQuery } from '../ux/shellNav'
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
type Pane = 'chat' | 'inbox' | 'tasks'
type SecondaryView = Exclude<Pane, 'chat'>
type Props = ChatPanelProps & { agent: Agent }

// Agent-to-agent messages and delegated tasks are secondary views of the conversation.
const PANE_LABELS = { inbox: '助手消息', tasks: '委派记录' } as const
const STATUS_LABELS: Record<string, string> = {
  queued: '排队中', leased: '处理中', done: '已完成', failed: '失败', dead: '已停止',
}
const KIND_LABELS: Record<string, string> = { message: '消息', task_request: '任务请求', task_result: '任务结果' }

export function AgentWorkspace({ agent, token, ...chatProps }: Props) {
  const [features, setFeatures] = useState<Features | null>(null)
  const [pane, setPane] = useState<Pane>('chat')
  const [conversation, setConversation] = useState(() => {
    try { return sessionStorage.getItem(`conveyor-agent-conversation:${agent.id}`) || agent.session_id }
    catch { return agent.session_id }
  })
  const [conversationReady, setConversationReady] = useState(false)
  const [chatBusy, setChatBusy] = useState(false)
  const chooseConversation = useCallback((session: string) => {
    setConversation(session)
    setPane('chat')
    try { sessionStorage.setItem(`conveyor-agent-conversation:${agent.id}`, session) } catch { /* optional persistence */ }
  }, [agent.id])
  const conversationLoaded = useCallback(() => setConversationReady(true), [])

  useEffect(() => {
    let alive = true
    setFeatures(null)
    setPane('chat')
    fetch(`/api/agent-screens/config?agent_id=${encodeURIComponent(agent.id)}`, {
      headers: { Authorization: `Bearer ${token}` },
    }).then(async response => response.ok ? response.json() as Promise<Features> : null)
      .then(data => { if (alive) setFeatures(data) })
      .catch(() => { if (alive) setFeatures(null) })
    return () => { alive = false }
  }, [agent.id, token])

  const views = (['inbox', 'tasks'] as const).filter(view => features?.enabled && features[view])
  // Phones: the conversation title gets the row; the two record views fold into a "⋯" menu.
  const compactBar = useMediaQuery('(max-width: 480px)')
  const toggleView = (view: SecondaryView) => setPane(current => current === view ? 'chat' : view)

  const chat = conversationReady && <ChatPanel key={`${agent.id}:${conversation}`} {...chatProps} token={token}
    agentSessionId={conversation} agentName={agent.name} agentColor={agent.color} onBusyChange={setChatBusy}
    active={chatProps.active !== false && pane === 'chat'} />

  return (
    <section className="agent-screen" aria-label={`和 ${agent.name} 的对话`}>
      <div className="chat-conversation-bar">
        <ConversationPicker key={agent.id} token={token} agentId={agent.id}
          defaultSession={agent.session_id} selected={conversation} busy={chatBusy}
          onSelect={chooseConversation} onReady={conversationLoaded} />
        {views.length > 0 && (compactBar
          ? <SecondaryViewsMenu views={views} pane={pane} onPick={toggleView} />
          : <div className="chat-secondary-views" role="group" aria-label="其他记录">
            {views.map(view => (
              <button type="button" key={view} className="chat-secondary-button"
                aria-pressed={pane === view}
                onClick={() => toggleView(view)}>{PANE_LABELS[view]}</button>
            ))}
          </div>)}
      </div>
      <div style={{ display: pane === 'chat' ? 'flex' : 'none', flexDirection: 'column', flex: 1, minWidth: 0, minHeight: 0 }}>
        {chat}
      </div>
      {pane !== 'chat' && <AgentMailboxTab key={`${agent.id}:${pane}`} token={token}
        agentId={agent.id} tab={pane} onBack={() => setPane('chat')} />}
    </section>
  )
}

/** Narrow screens: 助手消息 / 委派记录 behind one "⋯" button, so the conversation title keeps its room. */
function SecondaryViewsMenu({ views, pane, onPick }: {
  views: SecondaryView[]; pane: Pane; onPick: (view: SecondaryView) => void
}) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const menuId = useId()
  const items = () => [...(rootRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitemcheckbox"]') || [])]

  // Opening focuses the current view (or the first one); a tap anywhere else closes the menu.
  useEffect(() => {
    if (!open) return
    const options = items()
    ;(options.find(option => option.getAttribute('aria-checked') === 'true') || options[0])?.focus()
    const onPointer = (event: PointerEvent) => { if (!rootRef.current?.contains(event.target as Node)) setOpen(false) }
    document.addEventListener('pointerdown', onPointer, true)
    return () => document.removeEventListener('pointerdown', onPointer, true)
  }, [open])

  const onMenuKey = (event: KeyboardEvent<HTMLDivElement>) => {
    const options = items()
    const index = options.indexOf(document.activeElement as HTMLButtonElement)
    const move = (next: number) => { event.preventDefault(); options[(next + options.length) % options.length]?.focus() }
    if (event.key === 'ArrowDown') move(index + 1)
    else if (event.key === 'ArrowUp') move(index - 1)
    else if (event.key === 'Home') move(0)
    else if (event.key === 'End') move(options.length - 1)
    else if (event.key === 'Escape') { event.preventDefault(); setOpen(false); buttonRef.current?.focus() }
    else if (event.key === 'Tab') setOpen(false)
  }

  const current = pane === 'chat' ? null : pane
  return <div className="chat-secondary-menu" ref={rootRef}>
    <button ref={buttonRef} type="button" className={`chat-secondary-more ${current ? 'is-active' : ''}`}
      aria-label={current ? `其他记录（正在看${PANE_LABELS[current]}）` : '其他记录'}
      title={views.map(view => PANE_LABELS[view]).join('、')}
      aria-haspopup="menu" aria-expanded={open} aria-controls={open ? menuId : undefined}
      onClick={() => setOpen(value => !value)}>
      <svg aria-hidden="true" viewBox="0 0 20 20" width="18" height="18" fill="currentColor">
        <circle cx="4.5" cy="10" r="1.6" /><circle cx="10" cy="10" r="1.6" /><circle cx="15.5" cy="10" r="1.6" />
      </svg>
    </button>
    {open && <div id={menuId} className="chat-secondary-popover" role="menu" aria-label="其他记录" onKeyDown={onMenuKey}>
      {views.map(view => (
        <button key={view} type="button" role="menuitemcheckbox" aria-checked={pane === view} className="chat-secondary-option"
          onClick={() => { setOpen(false); onPick(view); buttonRef.current?.focus() }}>
          <span>{PANE_LABELS[view]}</span>
          {pane === view && <svg className="chat-secondary-check" aria-hidden="true" viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M3.5 8.5l3 3 6-7" /></svg>}
        </button>
      ))}
    </div>}
  </div>
}

function AgentMailboxTab({ token, agentId, tab, onBack }: {
  token: string; agentId: string; tab: 'inbox' | 'tasks'; onBack: () => void
}) {
  const [items, setItems] = useState<(Message | Task)[]>([])
  const [names, setNames] = useState<Record<string, string>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<{ message: string; detail?: string } | null>(null)
  const [cancelling, setCancelling] = useState<string | null>(null)

  const reload = useCallback(async (signal?: AbortSignal) => {
    try {
      const query = new URLSearchParams({ agent_id: agentId, view: tab })
      const response = await fetch(`/api/agent-screens?${query}`, {
        headers: { Authorization: `Bearer ${token}` }, signal,
      })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new ChatRequestError(body.error || '', response.status)
      }
      const data = await response.json() as { items: (Message | Task)[] }
      if (!signal?.aborted) { setItems(data.items); setError(null) }
    } catch (reason) {
      if (!signal?.aborted) {
        setError({ message: `暂时无法读取${PANE_LABELS[tab]}`, detail: describeChatError(reason).detail })
      }
    } finally {
      if (!signal?.aborted) setLoading(false)
    }
  }, [token, agentId, tab])

  useEffect(() => {
    const controller = new AbortController()
    void reload(controller.signal)
    return () => controller.abort()
  }, [reload])

  // Agent ids are opaque; show the names the owner gave them.
  useEffect(() => {
    let alive = true
    fetch('/api/agents', { headers: { Authorization: `Bearer ${token}` } })
      .then(response => response.ok ? response.json() : null)
      .then((data: { agents?: Array<{ id: string; name: string }> } | null) => {
        if (alive && data?.agents) setNames(Object.fromEntries(data.agents.map(agent => [agent.id, agent.name])))
      })
      .catch(() => undefined)
    return () => { alive = false }
  }, [token])
  const nameOf = (id: string) => names[id] || (id === 'default' ? '默认助手' : id)

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
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new ChatRequestError(body.error || '', response.status)
      }
      await reload()
    } catch (reason) {
      setError({ message: '没能取消这个任务，请再试一次', detail: describeChatError(reason).detail })
    } finally { setCancelling(null) }
  }

  const status = (value: string) =>
    <span className={`chat-status-chip status-${value}`}>{STATUS_LABELS[value] || value}</span>
  const when = (value: number) => {
    const label = formatRelativeTime(value)
    return label ? <time dateTime={new Date(value * 1000).toISOString()} title={formatFullTime(value)}>{label}</time> : null
  }

  return <section className="agent-screen-content chat-mailbox" aria-label={PANE_LABELS[tab]}>
    <header className="agent-screen-content-header">
      <button type="button" className="chat-mailbox-back" onClick={onBack}><span aria-hidden="true">←</span> 返回对话</button>
      <h3>{PANE_LABELS[tab]}</h3>
      <button type="button" className="chat-mailbox-refresh" onClick={() => void reload()}>刷新</button>
    </header>
    <p className="chat-mailbox-note">{tab === 'inbox'
      ? '来自其他助手的消息，仅供参考，不能代你下指令或批准操作'
      : '这个助手委派给其他助手、或从其他助手接到的任务。'}</p>
    {loading && <p className="chat-mailbox-empty" role="status">加载中…</p>}
    {error && <div className="chat-mailbox-error" role="alert">
      <span title={error.detail}>{error.message}</span>
      <button type="button" onClick={() => { setError(null); void reload() }}>重试</button>
    </div>}
    {!loading && !error && !items.length && <p className="chat-mailbox-empty">{tab === 'inbox' ? '还没有助手消息。' : '还没有委派记录。'}</p>}
    {tab === 'inbox' ? (items as Message[]).map(item =>
      <article className="agent-screen-item" key={item.id}>
        <p className="agent-screen-meta">
          <strong>{nameOf(item.from_agent)}</strong><span aria-hidden="true">→</span><span className="chat-sr-only">发给</span>{nameOf(item.to_agent)}
          <span className="chat-mailbox-kind">{KIND_LABELS[item.kind] || item.kind}</span>
          {status(item.status)}
          {when(item.created_at)}
        </p>
        {/* Text child only. NEVER innerHTML, markdown or arbitrary HTML. */}
        <pre className="agent-screen-message">{item.body}</pre>
      </article>
    ) : (items as Task[]).map(item =>
      <article className="agent-screen-item" key={item.id}>
        <p className="agent-screen-meta">
          <strong>{nameOf(item.from_agent)}</strong><span aria-hidden="true">→</span><span className="chat-sr-only">委派给</span>{nameOf(item.to_agent)}
          {when(item.created_at)}
        </p>
        <div className="chat-task-row">
          {status(item.status)}
          <span>已尝试 {item.attempts} 次</span>
          <code title={item.id}>编号 {item.id.slice(0, 8)}</code>
          {['queued', 'leased', 'failed'].includes(item.status) &&
            <button type="button" className="chat-task-cancel" disabled={cancelling === item.id}
              onClick={() => void cancel(item.id)}>
              {cancelling === item.id ? '正在取消…' : '取消任务'}
            </button>}
        </div>
      </article>
    )}
  </section>
}
