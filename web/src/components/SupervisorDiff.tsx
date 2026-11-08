import { useEffect, useMemo, useRef, useState } from 'react'
import { ChangedFileList, DiffBody, DiffStat, type FileStat } from './TaskParts'
import { splitDiffByFile } from '../taskText'

// ---------------------------------------------------------------------------
// 「改动」：GET /api/supervisor/tasks/<id>/diff 与 GET /api/supervisor/goals/<id>/diff
// 的读取与展示。老板只看中文：读不到、工作目录没了、改动太多，都有一句白话。
// ---------------------------------------------------------------------------

export type SupervisorDiffFile = {
  path: string
  status: 'A' | 'M' | 'D' | 'R' | '?' | string
  additions: number
  deletions: number
}

export type SupervisorDiff = {
  task_id?: string
  goal_id?: string
  base_sha?: string
  head_sha?: string
  files: SupervisorDiffFile[]
  diff: string
  truncated?: boolean
  untracked?: string[]
}

export type SupervisorDiffState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'ready'; data: SupervisorDiff }
  /** 404 worktree_missing：这个工作目录已经被删掉了。 */
  | { status: 'missing' }
  | { status: 'error' }

export const DIFF_LOADING_NOTE = '正在读取这一轮的改动…'
export const DIFF_MISSING_NOTE = '这个工作目录已经不在了，看不到这一轮的改动。'
export const DIFF_ERROR_NOTE = '暂时读不到这一轮的改动，稍后再试。'
export const DIFF_EMPTY_NOTE = '这一轮没有改动文件。'
export const DIFF_TRUNCATED_NOTE = '改动太多，只显示前一部分。'

/** 超过 3 个文件时，每个文件的差异单独收起来，免得一屏全是代码。 */
const DIFF_COLLAPSE_FILE_COUNT = 3

/**
 * 选中一个任务 / 点开「查看改动」时才去读差异（懒加载），读过的按 `task:<id>` /
 * `goal:<id>` 缓存，来回切换不再重复请求。
 */
export function useSupervisorDiff(
  apiCall: <T>(path: string, init?: RequestInit) => Promise<T>,
  kind: 'task' | 'goal',
  id: string,
  active: boolean,
): SupervisorDiffState {
  const [state, setState] = useState<SupervisorDiffState>({ status: 'idle' })
  const cacheRef = useRef<Map<string, SupervisorDiff>>(new Map())
  const cacheKey = active && id ? `${kind}:${id}` : ''

  useEffect(() => {
    if (!cacheKey) {
      setState({ status: 'idle' })
      return
    }
    const cached = cacheRef.current.get(cacheKey)
    if (cached) {
      setState({ status: 'ready', data: cached })
      return
    }
    let cancelled = false
    const controller = new AbortController()
    setState({ status: 'loading' })
    const path = kind === 'goal'
      ? `/api/supervisor/goals/${encodeURIComponent(id)}/diff`
      : `/api/supervisor/tasks/${encodeURIComponent(id)}/diff`
    void (async () => {
      try {
        const data = await apiCall<SupervisorDiff>(path, { signal: controller.signal })
        if (cancelled || controller.signal.aborted) return
        cacheRef.current.set(cacheKey, data)
        setState({ status: 'ready', data })
      } catch (err) {
        if (cancelled || controller.signal.aborted) return
        const code = (err as { code?: string } | null)?.code
        setState({ status: code === 'worktree_missing' ? 'missing' : 'error' })
      }
    })()
    return () => {
      cancelled = true
      controller.abort()
    }
  }, [apiCall, cacheKey, id, kind])

  return state
}

/** 文件列表（含未跟踪的新文件）+ 每个文件的 +/-，再跟上逐行差异。 */
export function SupervisorDiffView({ diffState }: { diffState: SupervisorDiffState }) {
  if (diffState.status === 'idle') return null
  if (diffState.status === 'loading') return <p className="sup-muted-note">{DIFF_LOADING_NOTE}</p>
  if (diffState.status === 'missing') return <p className="sup-muted-note">{DIFF_MISSING_NOTE}</p>
  if (diffState.status === 'error') return <p className="sup-muted-note">{DIFF_ERROR_NOTE}</p>

  const diff = diffState.data
  const tracked = Array.isArray(diff.files) ? diff.files : []
  const untracked = Array.isArray(diff.untracked) ? diff.untracked : []
  // 未跟踪的文件就是新文件：文件芯片统一按「新增」显示（后端可能给 '?' 或 '??'）。
  const files = [
    ...tracked.map(file => ({ path: file.path, status: fileStatusChip(file.status) })),
    ...untracked.map(path => ({ path, status: 'A' })),
  ]
  const stats: Record<string, FileStat> = {}
  for (const file of tracked) {
    if (!file.path) continue
    stats[file.path] = { additions: file.additions || 0, deletions: file.deletions || 0 }
  }

  return (
    <div className="sup-changes">
      {diff.truncated && (
        <p className="sup-changes-note" role="status">{DIFF_TRUNCATED_NOTE}</p>
      )}
      {files.length > 0 ? (
        <ChangedFileList files={files} stats={stats} />
      ) : (
        <p className="sup-muted-note">{DIFF_EMPTY_NOTE}</p>
      )}
      <DiffBlocks diff={diff} />
    </div>
  )
}

function fileStatusChip(status?: string): string {
  const code = (status || 'M').trim().toUpperCase()
  return code === '?' || code === '??' ? 'A' : code
}

/** 文件少就直接展开差异；文件多就一个文件一个折叠，点开才看那一块的逐行差异。 */
function DiffBlocks({ diff }: { diff: SupervisorDiff }) {
  const rows = useMemo(() => {
    const byPath = new Map<string, SupervisorDiffFile>()
    for (const file of diff.files || []) {
      if (file?.path) byPath.set(file.path, file)
    }
    const chunks = splitDiffByFile(diff.diff)
    const fallback: { path: string; text: string }[] = diff.diff?.trim() ? [{ path: '', text: diff.diff }] : []
    const list = chunks.length > 0 ? chunks : fallback
    return list.map(chunk => {
      const file = chunk.path ? byPath.get(chunk.path) : undefined
      return {
        path: chunk.path,
        additions: file?.additions || 0,
        deletions: file?.deletions || 0,
        text: chunk.text,
      }
    })
  }, [diff])

  if (rows.length === 0) return null
  if (rows.length <= DIFF_COLLAPSE_FILE_COUNT) return <DiffBody diff={diff.diff || ''} />

  return (
    <div className="sup-changes-files">
      {rows.map((row, index) => (
        <details key={`${index}-${row.path}`} className="sup-changes-file">
          <summary>
            <span className="sup-changes-file-path">{row.path || '这一轮的改动'}</span>
            <DiffStat additions={row.additions} deletions={row.deletions} />
          </summary>
          <DiffBody diff={row.text} />
        </details>
      ))}
    </div>
  )
}
