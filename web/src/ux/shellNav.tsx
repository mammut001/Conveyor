import { useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react'

/* App frame helpers (shell area): views and URL routing, media queries, keyboard
 * helpers, error wording, icons and the lock confirmation. */

export type ShellView = 'tasks' | 'chat' | 'code' | 'inbox' | 'memory' | 'approvals' | 'browser' | 'skills' | 'workflows' | 'connectors' | 'secrets' | 'teammate' | 'workers'

export const SHELL_VIEWS: readonly ShellView[] = [
  'chat', 'tasks', 'code', 'inbox', 'approvals', 'memory', 'teammate', 'skills', 'workflows', 'connectors', 'browser', 'secrets', 'workers',
]

export const VIEW_LABELS: Record<ShellView, string> = {
  chat: '对话', tasks: '任务', code: '项目', inbox: '收件箱', approvals: '审批', memory: '记忆', teammate: '值守',
  skills: '技能', workflows: '工作流', connectors: '连接器', browser: '网站授权', secrets: '密钥', workers: '执行者',
}

/** One muted line under the page title. */
export const VIEW_SUBTITLES: Record<ShellView, string> = {
  chat: '与 Conveyor 直接对话', tasks: '任务', code: '仓库、工作目录与派发任务', inbox: '定时任务的结果和通知', approvals: '等待你确认的操作',
  memory: '助手长期记住的信息', teammate: '全天候值守，发现问题及时提醒', skills: '可在对话中使用的技能',
  workflows: '分步骤推进的长任务', connectors: '连接外部工具和服务', browser: '允许助手访问的网站',
  secrets: '只供助手使用的私密凭据', workers: '谁来替你干活：检测、登录、按额度自动轮换',
}

export function isShellView(value: string): value is ShellView {
  return (SHELL_VIEWS as readonly string[]).includes(value)
}

function legacyPathView(pathname: string): ShellView | null {
  const segment = pathname.replace(/^\/+|\/+$/g, '')
  return isShellView(segment) ? segment : null
}

/** The view named by the URL: `#memory` (or `#/memory`), else a legacy path such as `/memory`. */
export function viewFromLocation(location: Pick<Location, 'hash' | 'pathname'> = window.location): ShellView | null {
  const hash = decodeURIComponent(location.hash.replace(/^#\/?/, '')).split(/[/?&]/)[0]
  if (isShellView(hash)) return hash
  return legacyPathView(location.pathname)
}

/** Same page with `#view`. The query string (`?agent=`, `?approval=`) is kept as is; a legacy `/view` path becomes `/`. */
export function urlForView(view: ShellView, location: Pick<Location, 'pathname' | 'search'> = window.location): string {
  const path = legacyPathView(location.pathname) ? '/' : location.pathname
  return `${path}${location.search}#${view}`
}

export function useMediaQuery(query: string): boolean {
  const read = () => typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia(query).matches
  const [matches, setMatches] = useState(read)
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return
    const list = window.matchMedia(query)
    const update = () => setMatches(list.matches)
    update()
    list.addEventListener('change', update)
    return () => list.removeEventListener('change', update)
  }, [query])
  return matches
}

export const SHORTCUT_PREFIX = typeof navigator !== 'undefined' && /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent) ? '⌥' : 'Alt+'

/** True when the key press belongs to a field (text inputs, textareas, selects, editable content). */
export function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  if (target.isContentEditable) return true
  if (target.tagName === 'TEXTAREA' || target.tagName === 'SELECT') return true
  if (target.tagName === 'INPUT') {
    return !['button', 'checkbox', 'radio', 'submit', 'reset', 'range', 'color', 'file', 'image'].includes((target as HTMLInputElement).type)
  }
  return false
}

/** The message box of the view on screen (the composers belong to the chat and tasks areas). */
export function findVisibleComposer(): HTMLTextAreaElement | null {
  const boxes = Array.from(document.querySelectorAll<HTMLTextAreaElement>('.stream-panel .composer textarea'))
  return boxes.find(box => !box.disabled && !box.readOnly && box.getClientRects().length > 0) || null
}

export type ConnectionState = 'connecting' | 'live' | 'reconnecting' | 'offline'
export const CONNECTION_LABELS: Record<ConnectionState, string> = { connecting: '连接中', live: '在线', reconnecting: '重连中', offline: '离线' }

const CJK = /[㐀-鿿]/
const KNOWN_ERRORS: [RegExp, string][] = [
  [/^unauthori[sz]ed$|^forbidden$/i, '登录已失效，请重新解锁控制台'],
  [/unexpected (end of json|token)|is not valid json|in json at position|cannot read propert|is not a function|is not iterable|bad shape/i, '数据格式不对，请稍后重试'],
  [/internal server error|traceback/i, '服务器暂时出错，请稍后重试'],
  [/^token rejected$/i, '访问令牌已失效，请重新解锁'],
  [/^(failed to fetch|load failed|networkerror.*|network error|could not connect|the network connection was lost\.?)$/i, '无法连接到服务器，正在自动重试…'],
  [/^realtime unavailable$/i, '实时进度暂时不可用，正在重新连接'],
  [/^submit failed$/i, '发送失败，请重试'],
  [/^action failed$/i, '操作没有完成，请重试'],
  [/^could not archive session$/i, '无法归档会话，请重试'],
  [/^could not load settings$/i, '无法读取设置，请稍后重试'],
  [/^could not save settings$/i, '设置没有保存，请重试'],
]

/** A short Chinese sentence for the error banner, plus the original wording as a muted detail when useful. */
export function shellErrorText(message: string): { text: string; detail?: string } {
  const raw = message.trim().replace(/^(Type)?Error:\s*/, '')
  for (const [pattern, text] of KNOWN_ERRORS) if (pattern.test(raw)) return { text }
  const status = raw.match(/^Request failed \((\d{3})\)$/i)
  if (status) return { text: Number(status[1]) >= 500 ? '服务器暂时出错，请稍后重试' : '请求没有完成，请检查后重试', detail: `HTTP ${status[1]}` }
  if (CJK.test(raw)) return { text: raw }
  const line = raw.split(/\r?\n/)[0].trim()
  const technical = !line || /traceback|\.py\b|node_modules/i.test(raw)
  return { text: '操作没有完成，请稍后重试', detail: technical ? undefined : line.length > 80 ? `${line.slice(0, 79)}…` : line }
}

export type ShellIconName = ShellView | 'more' | 'settings' | 'lock' | 'panel' | 'agents' | 'chevron'

const ICON_PATHS: Record<ShellIconName, string> = {
  chat: 'M20 11.5a7.5 7.5 0 0 1-10.9 6.7L4.5 19.5l1.3-4.1A7.5 7.5 0 1 1 20 11.5z',
  tasks: 'M9.5 6.5H20M9.5 12H20M9.5 17.5H20M4 6.5l1.2 1.2L7.4 5.5M4 12l1.2 1.2 2.2-2.2M4 17.5l1.2 1.2 2.2-2.2',
  code: 'M8 9l-4 3 4 3M16 9l4 3-4 3M13.5 6l-3 12',
  inbox: 'M3.5 13.5h5l1.5 2.5h4l1.5-2.5h5M6 5h12l2.5 8.5V19h-17v-5.5L6 5z',
  approvals: 'M12 20.5a8.5 8.5 0 1 0 0-17 8.5 8.5 0 0 0 0 17zM8.2 12.2l2.6 2.6 5-5.6',
  memory: 'M6 4.5h10.5A2.5 2.5 0 0 1 19 7v12.5H8.5A2.5 2.5 0 0 1 6 17V4.5zM6 17a2.5 2.5 0 0 1 2.5-2.5H19M10 8.5h5',
  teammate: 'M12 3.5l7 2.8v5.4c0 4.4-3 7.6-7 8.8-4-1.2-7-4.4-7-8.8V6.3l7-2.8zM8.8 12l2.2 2.2 4.2-4.4',
  skills: 'M4.5 19.5l9-9M14.5 3.5v3M13 5h3M19.5 8.5v2.5M18.2 9.8h2.6M9 3.5v2M8 4.5h2M15.8 8.2l-1.1-1.1',
  workflows: 'M4.5 5.5h5v5h-5zM14.5 13.5h5v5h-5zM9.5 8h3.5a2 2 0 0 1 2 2v3.5M4.5 18.5h5',
  connectors: 'M9 3.5v4M15 3.5v4M7 7.5h10v3.5a5 5 0 0 1-10 0V7.5zM12 16v4.5',
  browser: 'M12 20.5a8.5 8.5 0 1 0 0-17 8.5 8.5 0 0 0 0 17zM3.5 12h17M12 3.5c2.3 2.4 3.5 5.2 3.5 8.5s-1.2 6.1-3.5 8.5c-2.3-2.4-3.5-5.2-3.5-8.5S9.7 5.9 12 3.5z',
  secrets: 'M8.5 20a4.5 4.5 0 1 0 0-9 4.5 4.5 0 0 0 0 9zM11.7 12.3L20 4M16.5 7.5l2.5 2.5M14.5 9.5l1.8 1.8',
  workers: 'M12 2a2 2 0 0 1 2 2v1h2a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h2V4a2 2 0 0 1 2-2zm-3 8v2m6-2v2m-5 4h4',
  more: 'M5.5 12h.01M12 12h.01M18.5 12h.01',
  settings: 'M4 7h8.5M16.5 7H20M4 12h2.5M10.5 12H20M4 17h10.5M18.5 17H20M14.5 9a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM8.5 14a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM16.5 19a2 2 0 1 0 0-4 2 2 0 0 0 0 4z',
  lock: 'M6.5 11h11v9h-11zM8.8 11V8.3a3.2 3.2 0 0 1 6.4 0V11',
  panel: 'M4.5 5h15v14h-15zM14.5 5v14',
  agents: 'M9 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7zM3 20a6 6 0 0 1 12 0M16 4.3a3.5 3.5 0 0 1 0 6.4M18 14.4a6 6 0 0 1 3 5.6',
  chevron: 'M7 10l5 5 5-5',
}

export function ShellIcon({ name, size = 18 }: { name: ShellIconName; size?: number }) {
  return (
    <svg className="shell-icon" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth={name === 'more' ? 3 : 1.8} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
      <path d={ICON_PATHS[name]} />
    </svg>
  )
}

/** Confirmation before the console forgets its token. Esc and 取消 keep the session. */
export function LockConfirmDialog({ onCancel, onConfirm }: { onCancel: () => void; onConfirm: () => void }) {
  const cancelRef = useRef<HTMLButtonElement>(null)
  const onCancelRef = useRef(onCancel)
  onCancelRef.current = onCancel
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null
    cancelRef.current?.focus()
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' && event.key !== 'Esc') return
      event.preventDefault()
      event.stopPropagation()
      onCancelRef.current()
    }
    window.addEventListener('keydown', onKeyDown, true)
    return () => {
      window.removeEventListener('keydown', onKeyDown, true)
      if (previous?.isConnected) previous.focus({ preventScroll: true })
    }
  }, [])
  const trapTab = (event: ReactKeyboardEvent<HTMLElement>) => {
    if (event.key !== 'Tab') return
    const buttons = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('button:not([disabled])'))
    if (!buttons.length) return
    const first = buttons[0]
    const last = buttons[buttons.length - 1]
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
  }
  return (
    <div className="shell-confirm-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) onCancel() }}>
      <section className="shell-confirm" role="alertdialog" aria-modal="true" aria-labelledby="shell-lock-title" aria-describedby="shell-lock-desc" onKeyDown={trapTab}>
        <span className="shell-confirm-icon"><ShellIcon name="lock" size={20} /></span>
        <h2 id="shell-lock-title">锁定控制台？</h2>
        <p id="shell-lock-desc">将清除这台设备上保存的访问令牌。再次使用时，需要重新输入令牌解锁。</p>
        <div className="shell-confirm-actions">
          <button ref={cancelRef} type="button" className="shell-button" onClick={onCancel}>取消</button>
          <button type="button" className="shell-button shell-button--danger" onClick={onConfirm}>锁定</button>
        </div>
      </section>
    </div>
  )
}
