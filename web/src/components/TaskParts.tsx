import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { diffLines, fileStatusInfo, splitPath, toolDisplayName, toolStatusLabel, type ToolCall } from '../taskText'

export type FileStat = { additions: number; deletions: number }

/** Changed files: status chip, file name first (folder muted), per-file +/- when known. */
export function ChangedFileList({ files, stats }: {
  files: { status: string; path: string }[]
  stats?: Record<string, FileStat>
}) {
  return (
    <ul className="changed-files" aria-label="改动的文件">
      {files.map((file, index) => {
        const path = typeof file?.path === 'string' ? file.path.trim() : ''
        if (!path) return null
        const info = fileStatusInfo(file.status)
        const parts = splitPath(path)
        const target = parts.dir ? `${parts.dir}/${parts.name}` : parts.name
        const stat = stats?.[target] || stats?.[path]
        return (
          <li key={`${index}-${path}`} className="changed-file" title={parts.from ? `${parts.from} → ${target}` : target}>
            <span className={`file-chip file-chip--${info.tone}`}>{info.label}</span>
            <span className="changed-file-path">
              <span className="changed-file-name">{parts.name}</span>
              {parts.dir && <span className="changed-file-dir">{parts.dir}</span>}
            </span>
            {stat && <DiffStat additions={stat.additions} deletions={stat.deletions} />}
          </li>
        )
      })}
    </ul>
  )
}

export function DiffStat({ additions, deletions }: FileStat) {
  if (!additions && !deletions) return null
  return (
    <span className="diff-stat" aria-label={`新增 ${additions} 行，删除 ${deletions} 行`}>
      {additions > 0 && <span className="diff-stat-add">+{additions}</span>}
      {deletions > 0 && <span className="diff-stat-del">−{deletions}</span>}
    </span>
  )
}

/** Unified diff with added / removed lines coloured; scrolls inside its own box. */
export function DiffBody({ diff, loading, failed }: { diff: string; loading?: boolean; failed?: boolean }) {
  const parsed = useMemo(() => diffLines(diff), [diff])
  if (parsed.empty) {
    const note = loading ? '正在读取差异…'
      : failed ? '暂时读不到差异，稍后再试。'
      : '没有可显示的差异。新建的文件不在这里显示，可以看上面的文件列表。'
    return <pre className="diff-body diff-body--empty">{note}</pre>
  }
  return (
    <pre className="diff-body" tabIndex={0} aria-label="差异内容">
      <span className="diff-lines">
        {parsed.lines.map((line, index) => (
          <span key={index} className={`diff-line diff-line--${line.kind}`}>{line.text || ' '}</span>
        ))}
      </span>
      {parsed.truncated && <span className="diff-truncated">差异太长，只显示前 4000 行。</span>}
    </pre>
  )
}

/** Long text clamped to a few lines with 展开 / 收起 (only when it actually overflows). */
export function ClampedText({ text, lines = 4, className = '' }: { text: string; lines?: number; className?: string }) {
  const ref = useRef<HTMLParagraphElement>(null)
  const [expanded, setExpanded] = useState(false)
  const [overflowing, setOverflowing] = useState(false)
  useEffect(() => { setExpanded(false) }, [text])
  useLayoutEffect(() => {
    const element = ref.current
    if (!element || expanded) return
    const measure = () => setOverflowing(element.scrollHeight > element.clientHeight + 1)
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measure)
    observer.observe(element)
    return () => observer.disconnect()
  }, [text, lines, expanded])
  return (
    <div className="clamp-text">
      <p ref={ref} className={`${className}${expanded ? '' : ' is-clamped'}`} style={expanded ? undefined : { WebkitLineClamp: lines }}>{text}</p>
      {(overflowing || expanded) && (
        <button type="button" className="text-toggle" aria-expanded={expanded} onClick={() => setExpanded(value => !value)}>
          {expanded ? '收起' : '展开'}
        </button>
      )}
    </div>
  )
}

/** One tool call: readable name, Chinese status, output collapsed until asked for. */
export function ToolCallCard({ call, jobFinished, time }: { call: ToolCall; jobFinished: boolean; time: string }) {
  const name = toolDisplayName(call.name)
  const raw = call.name && name !== call.name ? call.name : ''
  const tone = call.status === 'running' && jobFinished ? 'ended' : call.status
  const output = call.output.slice(0, 12_000)
  const head = <>
    <span className={`tool-call-dot ${tone}`} aria-hidden="true" />
    <span className="tool-call-title"><strong>{name}</strong>{raw && <code>{raw}</code>}</span>
    <span className={`tool-call-status ${tone}`}>{toolStatusLabel(call.status, jobFinished)}</span>
    <time className="tool-call-time">{time}</time>
  </>
  if (!output && !call.error) return <div className={`tool-call ${tone}`}><div className="tool-call-row">{head}</div></div>
  return (
    <details className={`tool-call ${tone}`}>
      <summary className="tool-call-row">{head}<span className="tool-call-chevron" aria-hidden="true">⌄</span></summary>
      {call.error && <p className="tool-call-error">{call.error}</p>}
      {output && <pre className="tool-call-output">{output}</pre>}
    </details>
  )
}
