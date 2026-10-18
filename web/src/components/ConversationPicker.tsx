import { useEffect, useRef, useState } from 'react'

type Conversation = { id: string; title: string; origin_channel: string; session_id: string }
type Props = { token: string; agentId: string; defaultSession: string; busy: boolean;
  selected: string; onSelect: (session: string) => void; onReady: () => void }
const labels: Record<string, string> = { web: 'Web', telegram: 'Telegram', feishu: '飞书' }

export function ConversationPicker({ token, agentId, defaultSession, selected, busy, onSelect, onReady }: Props) {
  const [items, setItems] = useState<Conversation[]>([])
  const [error, setError] = useState('')
  const [creating, setCreating] = useState(false)
  const [title, setTitle] = useState('')
  const [showCreate, setShowCreate] = useState(false)
  const [loading, setLoading] = useState(true)
  const [revision, setRevision] = useState(0)
  const selectedRef = useRef(selected)
  selectedRef.current = selected
  useEffect(() => {
    const controller = new AbortController()
    let disposed = false
    setLoading(true); setError('')
    const timeout = setTimeout(() => controller.abort(), 10_000)
    fetch(`/api/agent-conversations?agent_id=${encodeURIComponent(agentId)}`, {
      headers: { Authorization: `Bearer ${token}` }, signal: controller.signal,
    }).then(async response => {
      if (response.status === 404) return []
      if (!response.ok) throw new Error('暂时无法读取会话')
      const body = await response.json()
      return Array.isArray(body.conversations) ? body.conversations as Conversation[] : []
    }).then(rows => {
      if (disposed) return
      setItems(rows)
      // Never silently continue a stale selection from a different Agent.
      if (!rows.some(row => row.session_id === selectedRef.current)) onSelect(defaultSession)
      onReady()
    }).catch(reason => {
      if (!disposed) setError(controller.signal.aborted ? '读取会话超时，请重试' : String(reason))
    }).finally(() => { clearTimeout(timeout); if (!disposed) setLoading(false) })
    return () => { disposed = true; clearTimeout(timeout); controller.abort() }
  }, [agentId, token, defaultSession, onSelect, onReady, revision])

  const create = async () => {
    if (busy || creating) return
    setCreating(true); setError('')
    try {
      const response = await fetch('/api/agent-conversations/new', {
        method: 'POST', headers: { Authorization: `Bearer ${token}`,
          'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
        body: JSON.stringify({ agent_id: agentId, title: title.trim() || 'Web 新话题' }),
      })
      if (!response.ok) throw new Error('创建会话失败，请重试')
      const body = await response.json()
      const row = body.conversation as Conversation
      if (!row || typeof row.session_id !== 'string') throw new Error('会话信息无效')
      setItems(old => [row, ...old]); onSelect(row.session_id); setTitle(''); setShowCreate(false)
    } catch (reason) { setError(String(reason)) }
    finally { setCreating(false) }
  }
  if (!items.length && !error && !loading) return null
  return <div className="conversation-picker">
    {loading && <span role="status">正在读取会话…</span>}
    {!!items.length && <>
      <label className="conversation-picker-current"><span>当前会话</span><select aria-label="选择要接续的会话" value={selected} disabled={busy || creating}
        onChange={event => onSelect(event.target.value)}>
        {items.map(row => <option key={row.id} value={row.session_id}>
          {labels[row.origin_channel] || '其他入口'} · {row.title}
        </option>)}
      </select></label>
      <button className="conversation-new-toggle" type="button" disabled={busy || creating}
        aria-expanded={showCreate} onClick={() => setShowCreate(value => !value)}>另开话题</button>
      {showCreate && <form className="conversation-create" onSubmit={event => { event.preventDefault(); void create() }}>
        <label htmlFor="new-conversation-title">给新话题起个名字</label>
        <div>
          <input id="new-conversation-title" aria-label="新话题标题" placeholder="例如：发布计划" value={title} maxLength={80}
            autoFocus disabled={busy || creating} onChange={event => setTitle(event.target.value)}
            onKeyDown={event => { if (event.key === 'Escape' && !creating) setShowCreate(false) }} />
          <button type="submit" disabled={busy || creating}>{creating ? '创建中…' : '创建话题'}</button>
          <button type="button" disabled={creating} onClick={() => setShowCreate(false)}>取消</button>
        </div>
      </form>}
      <small>各入口的历史独立 · 选择会话即可接着聊</small>
    </>}
    {error && <>
      <span role="alert">{error}</span>
      <button type="button" onClick={() => setRevision(old => old + 1)}>重试读取会话</button>
      <button type="button" disabled={busy} onClick={() => { onSelect(defaultSession); onReady() }}>打开 Web 原有会话</button>
    </>}
  </div>
}
