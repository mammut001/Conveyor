import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react'
import { CHANNEL_LABELS, ChatRequestError, describeChatError, describeNewConversationError, formatDayTime, formatRelativeTime } from './chatFormat'

export type Conversation = {
  id: string; title: string; origin_channel: string; session_id: string
  // Optional: shown when the server provides it.
  updated_at?: string | number; last_activity?: string | number; created_at?: string | number
}
type Props = { token: string; agentId: string; defaultSession: string; busy: boolean;
  selected: string; onSelect: (session: string) => void; onReady: () => void;
  onItemsChange?: (items: Conversation[]) => void }
type PickerError = { message: string; detail?: string }

// Server-made titles repeat the channel that the list already shows.
const DEFAULT_TITLES: Record<string, string> = {
  'Web 会话（原有记录）': '主会话', 'Telegram 独立会话': '主会话', '飞书独立会话': '主会话',
  'Web 新话题': '新会话', '新话题': '新会话',
}
export const channelOf = (row: Conversation) => CHANNEL_LABELS[row.origin_channel] || '其他入口'
export const titleOf = (row: Conversation) => DEFAULT_TITLES[row.title] || row.title
const timeOf = (row: Conversation) => formatRelativeTime(row.updated_at ?? row.last_activity ?? row.created_at)

function Chevron() {
  return <svg className="chat-picker-chevron" aria-hidden="true" viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d="M4 6l4 4 4-4" /></svg>
}

export function ConversationPicker({ token, agentId, defaultSession, selected, busy, onSelect, onReady, onItemsChange }: Props) {
  const [items, setItems] = useState<Conversation[]>([])
  const onItemsChangeRef = useRef(onItemsChange)
  onItemsChangeRef.current = onItemsChange
  const [error, setError] = useState<PickerError | null>(null)
  const [creating, setCreating] = useState(false)
  const [title, setTitle] = useState('')
  const [showCreate, setShowCreate] = useState(false)
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(true)
  const [revision, setRevision] = useState(0)
  const selectedRef = useRef(selected)
  selectedRef.current = selected
  const pickerRef = useRef<HTMLDivElement>(null)
  const createRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const newButtonRef = useRef<HTMLButtonElement>(null)
  const listRef = useRef<HTMLDivElement>(null)
  const listId = useId()
  const titleId = useId()

  useEffect(() => {
    const controller = new AbortController()
    let disposed = false
    setLoading(true); setError(null)
    const timeout = setTimeout(() => controller.abort(), 10_000)
    fetch(`/api/agent-conversations?agent_id=${encodeURIComponent(agentId)}`, {
      headers: { Authorization: `Bearer ${token}` }, signal: controller.signal,
    }).then(async response => {
      if (response.status === 404) return []
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new ChatRequestError(body.error || '', response.status)
      }
      const body = await response.json()
      return Array.isArray(body.conversations) ? body.conversations as Conversation[] : []
    }).then(rows => {
      if (disposed) return
      setItems(rows)
      onItemsChangeRef.current?.(rows)
      // Never silently continue a stale selection from a different Agent.
      if (!rows.some(row => row.session_id === selectedRef.current)) onSelect(defaultSession)
      onReady()
    }).catch(reason => {
      if (disposed) return
      setError(controller.signal.aborted
        ? { message: '读取会话列表超时了，可以重试' }
        : { message: '暂时无法读取会话列表', detail: describeChatError(reason).detail })
    }).finally(() => { clearTimeout(timeout); if (!disposed) setLoading(false) })
    return () => { disposed = true; clearTimeout(timeout); controller.abort() }
  }, [agentId, token, defaultSession, onSelect, onReady, revision])

  // A reply in progress locks the conversation; close anything still open.
  useEffect(() => { if (busy) { setOpen(false); setShowCreate(false) } }, [busy])

  // Click or tap anywhere else closes the open popover.
  useEffect(() => {
    if (!open && !showCreate) return
    const onPointer = (event: PointerEvent) => {
      const target = event.target as Node
      if (open && !pickerRef.current?.contains(target)) setOpen(false)
      if (showCreate && !createRef.current?.contains(target) && !creating) setShowCreate(false)
    }
    document.addEventListener('pointerdown', onPointer, true)
    return () => document.removeEventListener('pointerdown', onPointer, true)
  }, [open, showCreate, creating])

  // Focus the current conversation when the list opens.
  useEffect(() => {
    if (!open) return
    const options = [...(listRef.current?.querySelectorAll<HTMLButtonElement>('[role="option"]') || [])]
    ;(options.find(option => option.getAttribute('aria-selected') === 'true') || options[0])?.focus()
  }, [open])

  const closeCreate = () => { setShowCreate(false); newButtonRef.current?.focus() }

  const choose = (session: string) => {
    setOpen(false)
    if (session !== selected) onSelect(session)
    triggerRef.current?.focus()
  }

  const onListKey = (event: KeyboardEvent<HTMLDivElement>) => {
    const options = [...(listRef.current?.querySelectorAll<HTMLButtonElement>('[role="option"]') || [])]
    const index = options.indexOf(document.activeElement as HTMLButtonElement)
    const move = (next: number) => { event.preventDefault(); options[Math.max(0, Math.min(options.length - 1, next))]?.focus() }
    if (event.key === 'ArrowDown') move(index + 1)
    else if (event.key === 'ArrowUp') move(index - 1)
    else if (event.key === 'Home') move(0)
    else if (event.key === 'End') move(options.length - 1)
    else if (event.key === 'Escape') { event.preventDefault(); setOpen(false); triggerRef.current?.focus() }
    else if (event.key === 'Tab') setOpen(false)
  }

  const create = async () => {
    if (busy || creating) return
    setCreating(true); setError(null)
    try {
      const response = await fetch('/api/agent-conversations/new', {
        method: 'POST', headers: { Authorization: `Bearer ${token}`,
          'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
        body: JSON.stringify({ agent_id: agentId, title: title.trim() || `新会话 ${formatDayTime(new Date())}` }),
      })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new ChatRequestError(body.error || '', response.status)
      }
      const body = await response.json()
      const row = body.conversation as Conversation
      if (!row || typeof row.session_id !== 'string') throw new ChatRequestError('invalid conversation')
      setItems(old => {
        const next = [row, ...old]
        onItemsChangeRef.current?.(next)
        return next
      })
      onSelect(row.session_id); setTitle(''); setShowCreate(false)
    } catch (reason) {
      const human = describeNewConversationError(reason)
      setError({ message: human.message, detail: human.detail })
    } finally { setCreating(false) }
  }

  if (!items.length && !error && !loading) return null
  const current = items.find(row => row.session_id === selected)
  return <div className="chat-conversation-picker">
    {loading && !items.length && <span className="chat-picker-loading" role="status">正在读取会话…</span>}
    {!!items.length && <>
      <div className="chat-picker" ref={pickerRef}>
        <button ref={triggerRef} type="button" className="chat-picker-trigger"
          aria-haspopup="listbox" aria-expanded={open} aria-controls={open ? listId : undefined}
          aria-label={`当前会话：${current ? `${channelOf(current)} · ${titleOf(current)}` : '未选择'}`}
          data-session={selected} disabled={busy || creating}
          title={busy ? '回复完成后才能切换会话' : '切换会话（各入口的记录相互独立）'}
          onClick={() => { setShowCreate(false); setOpen(value => !value) }}
          onKeyDown={event => { if (event.key === 'ArrowDown' && !open) { event.preventDefault(); setOpen(true) } }}>
          {current
            ? <><span className="chat-channel-tag">{channelOf(current)}</span><span className="chat-picker-title">{titleOf(current)}</span></>
            : <span className="chat-picker-title">选择会话</span>}
          <Chevron />
        </button>
        {open && <div ref={listRef} id={listId} className="chat-picker-menu" role="listbox" aria-label="选择会话" onKeyDown={onListKey}>
          {items.map(row => {
            const isCurrent = row.session_id === selected
            const time = timeOf(row)
            return <button key={row.id} type="button" role="option" aria-selected={isCurrent}
              className={`chat-picker-option ${isCurrent ? 'is-current' : ''}`} onClick={() => choose(row.session_id)}>
              <span className="chat-channel-tag">{channelOf(row)}</span>
              <span className="chat-picker-option-title">{titleOf(row)}</span>
              {time && <span className="chat-picker-option-time">{time}</span>}
              {isCurrent && <svg className="chat-picker-check" aria-hidden="true" viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M3.5 8.5l3 3 6-7" /></svg>}
            </button>
          })}
        </div>}
      </div>
      <div className="chat-new-conversation" ref={createRef}>
        <button ref={newButtonRef} className="chat-new-button" type="button" disabled={busy || creating}
          aria-expanded={showCreate} aria-label="新会话" title="开始一段新的会话"
          onClick={() => { setOpen(false); setShowCreate(value => !value) }}>
          {/* Narrow screens show only the ＋ (the label stays for screen readers). */}
          <span aria-hidden="true">＋</span><span className="chat-new-label">新会话</span>
        </button>
        {showCreate && <form className="chat-create-popover" onSubmit={event => { event.preventDefault(); void create() }}>
          <label htmlFor={titleId}>新会话名称</label>
          <input id={titleId} placeholder="例如：发布计划（可不填）" value={title} maxLength={80}
            autoFocus disabled={busy || creating} onChange={event => setTitle(event.target.value)}
            onKeyDown={event => { if (event.key === 'Escape' && !creating) { event.preventDefault(); closeCreate() } }} />
          <div className="chat-create-actions">
            <button type="button" disabled={creating} onClick={closeCreate}>取消</button>
            <button type="submit" className="chat-create-submit" disabled={busy || creating}>{creating ? '创建中…' : '创建'}</button>
          </div>
        </form>}
      </div>
    </>}
    {error && <div className="chat-picker-error" role="alert">
      <span title={error.detail}>{error.message}</span>
      <button type="button" onClick={() => items.length ? setError(null) : setRevision(old => old + 1)}>{items.length ? '关闭' : '重试'}</button>
      {!items.length && <button type="button" disabled={busy} onClick={() => { onSelect(defaultSession); onReady() }}>打开主会话</button>}
    </div>}
  </div>
}
