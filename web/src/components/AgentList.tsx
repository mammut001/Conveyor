import { FormEvent, KeyboardEvent as ReactKeyboardEvent, useCallback, useEffect, useId, useRef, useState } from 'react'

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
}

export const AGENT_COLORS = ['#2f7df6', '#f59e0b', '#f97316', '#8b5cf6', '#10b981', '#ec4899', '#a16207', '#64748b']
const COLOR_NAMES: Record<string, string> = {
  '#2f7df6': '蓝色', '#f59e0b': '琥珀色', '#f97316': '橙色', '#8b5cf6': '紫色',
  '#10b981': '绿色', '#ec4899': '粉色', '#a16207': '棕色', '#64748b': '灰色',
}

/** Last path segment: what the agent is "about", shown as a tag beside its name. */
export function agentTag(agent: Pick<Agent, 'workspace_path'>): string {
  return agent.workspace_path.replace(/\/+$/, '').split('/').pop() || ''
}

export function AgentAvatar({ agent, size = 44 }: { agent: Pick<Agent, 'name' | 'color'>; size?: number }) {
  return (
    <span className="agent-avatar" style={{ width: size, height: size, background: agent.color, fontSize: Math.round(size * 0.42) }} aria-hidden="true">
      {(Array.from(agent.name.trim())[0] || '?').toUpperCase()}
    </span>
  )
}

/** One line of plain text from a message that may contain Markdown (the server already joined its lines). */
function plainPreview(text: string): string {
  return text
    .replace(/```[\w+-]*/g, ' ') // code fences and their language tag
    .replace(/`([^`]*)`/g, '$1') // inline code
    .replace(/!\[([^\]]*)\]\([^)]*\)?/g, '$1') // images → alt text
    .replace(/\[([^\]]+)\]\((?:[^()]|\([^)]*\))*\)?/g, '$1') // links → their text (also when cut off)
    .replace(/<((?:https?|mailto):[^>\s]+)>/g, '$1') // autolinks
    .replace(/(^|\s)(?:#{1,6}|>+)(?=\s)/g, '$1') // headings, quotes
    .replace(/(^|\s)(?:[-*+]|\d{1,3}[.)])\s+\[[ xX]\](?=\s)/g, '$1') // task list boxes
    .replace(/^\s*[-*+]\s+/, '') // a leading bullet
    .replace(/\s[-*+](?=\s)/g, ' ·') // later bullets
    .replace(/\|?(?:\s*:?-{3,}:?\s*\|)+(?:\s*:?-{3,}:?\s*)?/g, ' ') // table separator rows
    .replace(/\s*\|\s*/g, ' ') // table cells
    .replace(/(\*\*|__)(\S(?:[\s\S]*?\S)?)\1/g, '$2') // bold
    .replace(/(^|[^\w*])\*(\S(?:[^*]*?\S)?)\*(?![\w*])/g, '$1$2') // italic *x*
    .replace(/(^|[^\w_])_(\S(?:[^_]*?\S)?)_(?![\w_])/g, '$1$2') // italic _x_
    .replace(/~~(\S(?:[\s\S]*?\S)?)~~/g, '$1') // strikethrough
    .replace(/\*\*|__|~~/g, '') // markers left open by the server's 160-character cut
    .replace(/\s+/g, ' ')
    .replace(/^[\s·]+/, '')
    .trim()
}

/** Short Chinese time for a list row: 刚刚 / 5 分钟前 / 3 小时前 / 昨天 / 3 天前 / 11月3日 (agent and session lists). */
export function relativeTime(value: string | null | undefined, now: number = Date.now()): string {
  if (!value) return ''
  const time = new Date(value).getTime()
  if (Number.isNaN(time)) return ''
  const diff = now - time
  if (diff < 60_000) return '刚刚'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`
  const startOfToday = new Date(now)
  startOfToday.setHours(0, 0, 0, 0)
  const days = Math.ceil((startOfToday.getTime() - time) / 86_400_000)
  if (days <= 1) return '昨天'
  if (days < 7) return `${days} 天前`
  const date = new Date(time)
  return date.getFullYear() === new Date(now).getFullYear()
    ? `${date.getMonth() + 1}月${date.getDate()}日`
    : `${date.getFullYear()}/${date.getMonth() + 1}/${date.getDate()}`
}

function fullTime(value?: string | null): string | undefined {
  const date = new Date(value || '')
  return Number.isNaN(date.getTime()) ? undefined
    : date.toLocaleString('zh-CN', { year: 'numeric', month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false })
}

const STATUS_LABEL: Record<Agent['status'], string> = { idle: '', working: '运行中', waiting: '待你确认' }

function PencilIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
      <path d="M10.9 2.6a1.6 1.6 0 0 1 2.3 2.3L5.6 12.5 2.5 13.5l1-3.1 7.4-7.8Z" />
      <path d="M9.6 4l2.4 2.4" />
    </svg>
  )
}

export function AgentList({ agents, selectedId, onSelect, onNew, onEdit, headerEnd }: {
  agents: Agent[]
  selectedId: string
  onSelect: (agent: Agent) => void
  onNew: () => void
  onEdit: (agent: Agent) => void
  /** Extra control at the end of the heading row (the phone drawer's close button). */
  headerEnd?: React.ReactNode
}) {
  const now = Date.now()
  return (
    <>
      <div className="panel-heading agent-list-heading">
        <h2>助手</h2>
        <button type="button" className="agent-new-btn" onClick={onNew} aria-label="新建助手" title="新建助手">
          <span aria-hidden="true">+</span> 新建
        </button>
        {headerEnd}
      </div>
      <div className="session-list agent-list">
        {agents.map(agent => {
          const active = agent.id === selectedId
          const plain = plainPreview(agent.last_message || '')
          const text = plain ? (agent.last_message_role === 'user' ? '你：' : '') + plain : '还没有对话'
          const time = relativeTime(agent.last_activity, now)
          const state = STATUS_LABEL[agent.status] || ''
          return (
            <div key={agent.id} className={`agent-row ${active ? 'is-active' : ''}`}>
              <button
                type="button"
                className={`agent-item ${active ? 'active' : ''} ${state ? `is-${agent.status}` : ''}`}
                aria-pressed={active}
                aria-current={active ? 'true' : undefined}
                title={`${agent.name}${agent.workspace_path ? ` · ${agent.workspace_path}` : ''}\n${text}`}
                onClick={() => onSelect(agent)}
                onContextMenu={event => { event.preventDefault(); onEdit(agent) }}
              >
                <span className="agent-item-avatar">
                  <AgentAvatar agent={agent} size={36} />
                  {state && <span className={`agent-status-dot ${agent.status}`} aria-hidden="true" />}
                </span>
                <span className="agent-item-text">
                  <span className="agent-item-title">
                    <strong>{agent.name}</strong>
                    {time && <time className="agent-item-time" dateTime={agent.last_activity || undefined} title={fullTime(agent.last_activity)}>{time}</time>}
                  </span>
                  <small className={`agent-item-preview ${plain ? '' : 'is-empty'}`}>
                    {state && <><span className={`agent-item-state ${agent.status}`}>{state}</span><span aria-hidden="true"> · </span></>}
                    {text}
                  </small>
                </span>
              </button>
              <button type="button" className="agent-edit-btn" title="编辑助手" aria-label={`编辑 ${agent.name}`} onClick={() => onEdit(agent)}>
                <PencilIcon />
              </button>
            </div>
          )
        })}
        {!agents.length && <div className="empty agent-list-empty">还没有助手，点“新建”添加一个。</div>}
      </div>
    </>
  )
}

export type AgentDraft = { name: string; instructions: string; workspace_path: string; color: string }
type AgentField = keyof AgentDraft
type FieldErrors = Partial<Record<AgentField, string>>

const NAME_MAX = 60
const INSTRUCTIONS_MAX = 4000
const PATH_MAX = 512
const FIELD_ORDER: AgentField[] = ['name', 'instructions', 'color', 'workspace_path']

function validateAgent(draft: AgentDraft): FieldErrors {
  const errors: FieldErrors = {}
  if (!draft.name.trim()) errors.name = '请填写名称'
  const folder = draft.workspace_path.trim()
  if (folder && !folder.startsWith('/')) errors.workspace_path = '请填写以 / 开头的完整路径，例如 /home/me/project'
  return errors
}

/** Server messages (English) → a short Chinese sentence, attached to a field when there is one. */
function describeAgentError(message: string, archiving: boolean): { field?: AgentField; text: string; detail?: string } {
  if (/^name must be/i.test(message)) return { field: 'name', text: `名称需要 1–${NAME_MAX} 个字` }
  if (/^instructions must be/i.test(message)) return { field: 'instructions', text: `职责说明最多 ${INSTRUCTIONS_MAX} 字` }
  if (/^workspace_path must be/i.test(message)) return { field: 'workspace_path', text: '请填写以 / 开头的完整路径，例如 /home/me/project' }
  if (/^color must/i.test(message)) return { field: 'color', text: '请重新选择一个头像颜色' }
  if (/^at most \d+ agents/i.test(message)) return { text: '助手数量已达上限（50 个），请先归档不用的助手。' }
  if (/default agent cannot be removed/i.test(message)) return { text: '默认助手不能归档。' }
  if (/^not found$/i.test(message)) return { text: '找不到这个助手，它可能已被归档。请刷新页面后再试。' }
  if (/failed to fetch|networkerror|load failed|network request failed/i.test(message)) return { text: '连不上服务器，请检查网络后重试。' }
  return { text: archiving ? '归档没有成功，请稍后再试。' : '保存没有成功，请稍后再试。', detail: message || undefined }
}

const FOCUSABLE = 'button:not([disabled]):not([tabindex="-1"]), input:not([disabled]):not([tabindex="-1"]), textarea:not([disabled]), select:not([disabled]), [href], [tabindex]:not([tabindex="-1"])'

/** Keep Tab inside the open dialog. */
function trapFocus(event: ReactKeyboardEvent<HTMLElement>) {
  if (event.key !== 'Tab') return
  const items = Array.from(event.currentTarget.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(item => item.offsetParent !== null)
  if (!items.length) { event.preventDefault(); return }
  const first = items[0]
  const last = items[items.length - 1]
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
}

/** Enter that only confirms an IME candidate (e.g. pinyin) must not submit the form. */
function holdImeEnter(event: ReactKeyboardEvent<HTMLInputElement>) {
  if (event.key === 'Enter' && (event.nativeEvent.isComposing || event.keyCode === 229)) event.preventDefault()
}

export function AgentDialog({ agent, onClose, onSave, onArchive }: {
  agent?: Agent
  onClose: () => void
  onSave: (draft: AgentDraft) => Promise<void>
  onArchive?: () => Promise<void>
}) {
  const [initial] = useState<AgentDraft>(() => ({
    name: agent?.name || '',
    instructions: agent?.instructions || '',
    workspace_path: agent?.workspace_path || '',
    color: agent?.color || AGENT_COLORS[Math.floor(Math.random() * AGENT_COLORS.length)],
  }))
  const [draft, setDraft] = useState<AgentDraft>(initial)
  const [busy, setBusy] = useState<'' | 'save' | 'archive'>('')
  const [error, setError] = useState<{ text: string; detail?: string } | null>(null)
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({})
  const [confirmingArchive, setConfirmingArchive] = useState(false)
  const id = useId()
  const nameRef = useRef<HTMLInputElement>(null)
  const instructionsRef = useRef<HTMLTextAreaElement>(null)
  const folderRef = useRef<HTMLInputElement>(null)
  const colorsRef = useRef<HTMLDivElement>(null)
  const archiveCancelRef = useRef<HTMLButtonElement>(null)
  const colors = AGENT_COLORS.includes(initial.color) ? AGENT_COLORS : [initial.color, ...AGENT_COLORS]
  const dirty = FIELD_ORDER.some(field => initial[field] !== draft[field])
  const canArchive = Boolean(agent && !agent.is_default && onArchive)

  const focusField = (field: AgentField) => {
    if (field === 'color') colorsRef.current?.querySelector<HTMLElement>('[tabindex="0"]')?.focus()
    else ({ name: nameRef, instructions: instructionsRef, workspace_path: folderRef })[field].current?.focus()
  }

  // Focus the name on open; hand focus back to whatever opened the dialog when it closes.
  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null
    nameRef.current?.focus()
    return () => { if (opener?.isConnected) opener.focus() }
  }, [])
  useEffect(() => { if (confirmingArchive) archiveCancelRef.current?.focus() }, [confirmingArchive])

  // Esc first backs out of the archive question, then closes (asking before dropping edits).
  const requestClose = useCallback(() => {
    if (confirmingArchive) { setConfirmingArchive(false); return }
    if (dirty && !busy && !window.confirm('放弃还没保存的修改？')) return
    onClose()
  }, [busy, confirmingArchive, dirty, onClose])
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.isComposing || event.keyCode === 229) return
      event.preventDefault()
      requestClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [requestClose])

  const update = (field: AgentField, value: string) => {
    setDraft(previous => ({ ...previous, [field]: value }))
    if (fieldErrors[field]) setFieldErrors(previous => ({ ...previous, [field]: undefined }))
  }

  const run = async (kind: 'save' | 'archive', action: () => Promise<void>) => {
    setBusy(kind); setError(null)
    try { await action(); onClose() }
    catch (reason) {
      const described = describeAgentError(reason instanceof Error ? reason.message : '', kind === 'archive')
      setConfirmingArchive(false)
      if (described.field) { setFieldErrors({ [described.field]: described.text }); focusField(described.field) }
      else setError({ text: described.text, detail: described.detail })
    }
    finally { setBusy('') }
  }
  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (busy) return
    const errors = validateAgent(draft)
    setFieldErrors(errors)
    const first = FIELD_ORDER.find(field => errors[field])
    if (first) { setError(null); focusField(first); return }
    void run('save', () => onSave({ ...draft, name: draft.name.trim(), workspace_path: draft.workspace_path.trim() }))
  }
  const moveColor = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    const step = event.key === 'ArrowRight' || event.key === 'ArrowDown' ? 1 : event.key === 'ArrowLeft' || event.key === 'ArrowUp' ? -1 : 0
    const index = Math.max(0, colors.indexOf(draft.color))
    const next = step ? (index + step + colors.length) % colors.length
      : event.key === 'Home' ? 0 : event.key === 'End' ? colors.length - 1 : -1
    if (next < 0) return
    event.preventDefault()
    update('color', colors[next])
    event.currentTarget.querySelectorAll<HTMLElement>('[role="radio"]')[next]?.focus()
  }
  // A field shows its error in place of its hint.
  const note = (field: AgentField, hint: React.ReactNode) => fieldErrors[field]
    ? <p className="field-error" id={`${id}-${field}-note`}>{fieldErrors[field]}</p>
    : <p className="field-hint" id={`${id}-${field}-note`}>{hint}</p>

  return (
    <div className="settings-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) requestClose() }}>
      <section className="settings-sheet agent-dialog" role="dialog" aria-modal="true" aria-labelledby={`${id}-title`} onKeyDown={trapFocus}>
        <header className="agent-dialog-header">
          <AgentAvatar agent={{ name: draft.name || '?', color: draft.color }} size={44} />
          <div>
            <h2 id={`${id}-title`}>{agent ? '编辑助手' : '新建助手'}</h2>
            <p>{draft.name.trim() || agent?.name || '先给它起个名字'}</p>
          </div>
          <button type="button" className="close-button" onClick={requestClose} aria-label="关闭">×</button>
        </header>
        <form onSubmit={submit} noValidate>
          <div className="sheet-body">
            <div className={`dialog-field ${fieldErrors.name ? 'has-error' : ''}`}>
              <label htmlFor={`${id}-name`}>名称</label>
              <input id={`${id}-name`} ref={nameRef} value={draft.name} maxLength={NAME_MAX} autoComplete="off"
                aria-invalid={Boolean(fieldErrors.name)} aria-describedby={`${id}-name-note`}
                onChange={event => update('name', event.target.value)} onKeyDown={holdImeEnter} placeholder="例如：财务助理" />
              {note('name', '显示在助手列表和对话顶部。')}
            </div>
            <div className={`dialog-field ${fieldErrors.instructions ? 'has-error' : ''}`}>
              <label htmlFor={`${id}-instructions`}>职责说明</label>
              <textarea id={`${id}-instructions`} ref={instructionsRef} value={draft.instructions} maxLength={INSTRUCTIONS_MAX} rows={5}
                aria-invalid={Boolean(fieldErrors.instructions)} aria-describedby={`${id}-instructions-note`}
                onChange={event => update('instructions', event.target.value)}
                onKeyDown={event => {
                  if (event.key === 'Enter' && (event.metaKey || event.ctrlKey) && !event.nativeEvent.isComposing) {
                    event.preventDefault(); event.currentTarget.form?.requestSubmit()
                  }
                }}
                placeholder="例如：负责整理每月账单和报销。回答先给结论，再列明细；涉及付款时先问我。" />
              {note('instructions', <>
                <span>它的角色和长期要求，每次回复、每个任务都会参考。</span>
                {draft.instructions.length >= INSTRUCTIONS_MAX - 500 && <span className="field-count">{draft.instructions.length}/{INSTRUCTIONS_MAX}</span>}
              </>)}
            </div>
            <div className={`dialog-field ${fieldErrors.color ? 'has-error' : ''}`}>
              <span className="dialog-label" id={`${id}-color-label`}>头像颜色</span>
              <div ref={colorsRef} className="agent-colors" role="radiogroup" aria-labelledby={`${id}-color-label`} aria-describedby={`${id}-color-note`} onKeyDown={moveColor}>
                {colors.map((color, index) => {
                  const checked = draft.color === color
                  return (
                    <button key={color} type="button" role="radio" aria-checked={checked} aria-label={COLOR_NAMES[color] || '当前颜色'}
                      tabIndex={checked || (!colors.includes(draft.color) && index === 0) ? 0 : -1}
                      className={checked ? 'selected' : ''} style={{ background: color }} onClick={() => update('color', color)} />
                  )
                })}
              </div>
              {note('color', '方便在列表里一眼认出它。')}
            </div>
            <div className={`dialog-field ${fieldErrors.workspace_path ? 'has-error' : ''}`}>
              <label htmlFor={`${id}-workspace_path`}>项目文件夹 <span className="field-optional">可选</span></label>
              <input id={`${id}-workspace_path`} ref={folderRef} className="mono-input" value={draft.workspace_path} maxLength={PATH_MAX}
                aria-invalid={Boolean(fieldErrors.workspace_path)} aria-describedby={`${id}-workspace_path-note`}
                onChange={event => update('workspace_path', event.target.value)} onKeyDown={holdImeEnter} placeholder="/home/conveyor/my-project"
                spellCheck={false} autoCapitalize="off" autoCorrect="off" autoComplete="off" />
              {note('workspace_path', '服务器上 Git 仓库的根目录，它的改代码任务会在这里进行；留空则用共享工作区。')}
            </div>
          </div>
          {confirmingArchive && agent ? (
            <footer className="agent-dialog-actions agent-archive-confirm" role="group" aria-labelledby={`${id}-archive-question`}>
              <p id={`${id}-archive-question`}>
                <strong>归档「{agent.name}」？</strong>
                <span>它会从助手列表中移除，不能再和它对话；对话记录和文件仍保留在服务器上，但目前无法在网页上恢复。</span>
              </p>
              <div className="agent-archive-buttons">
                <button type="button" ref={archiveCancelRef} disabled={Boolean(busy)} onClick={() => setConfirmingArchive(false)}>取消</button>
                <button type="button" className="danger-solid" disabled={Boolean(busy)} onClick={() => { if (onArchive) void run('archive', onArchive) }}>
                  {busy === 'archive' ? '正在归档…' : '确认归档'}
                </button>
              </div>
            </footer>
          ) : (
            <footer className="agent-dialog-actions">
              {error && <div className="error-banner dialog-error" role="alert"><span>{error.text}</span>{error.detail && <small>{error.detail}</small>}</div>}
              {canArchive && <button type="button" className="danger" disabled={Boolean(busy)} onClick={() => setConfirmingArchive(true)}>归档</button>}
              <span />
              <button type="button" disabled={Boolean(busy)} onClick={onClose}>取消</button>
              <button type="submit" className="primary" disabled={Boolean(busy)}>
                {busy === 'save' ? '正在保存…' : agent ? '保存' : '创建助手'}
              </button>
            </footer>
          )}
        </form>
      </section>
    </div>
  )
}
