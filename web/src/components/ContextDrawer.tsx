import { useEffect, useState } from 'react'
import { RuntimeOwnerCard } from './RuntimeOwnerCard'
import type { RuntimeOwner } from '../runtime'
import type { ComputerStatus, Job, NodeInfo, SystemStatus } from '../types'

export type DrawerKind = 'changes' | 'details' | 'computer' | 'system'

function formatTime(value?: string | null) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function bytes(value: number | null) {
  if (value == null) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']; let n = value; let i = 0
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++ }
  return `${n.toFixed(i > 1 ? 1 : 0)} ${units[i]}`
}

function KeyValue({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return <div className="v3-key-value"><span>{label}</span><strong className={mono ? 'mono' : ''}>{value}</strong></div>
}

function AuthenticatedImage({ artifact, token }: { artifact: ComputerStatus['screenshots'][number]; token: string }) {
  const [url, setUrl] = useState('')
  useEffect(() => {
    let active = true; let localUrl = ''
    void fetch(`/api/artifacts/${encodeURIComponent(artifact.artifact_id)}`, { headers: { Authorization: `Bearer ${token}` } })
      .then(response => response.ok ? response.blob() : Promise.reject())
      .then(blob => { if (active) { localUrl = URL.createObjectURL(blob); setUrl(localUrl) } })
      .catch(() => {})
    return () => { active = false; if (localUrl) URL.revokeObjectURL(localUrl) }
  }, [artifact.artifact_id, token])
  return url ? <figure className="v3-screenshot"><img src={url} alt="Latest Mac node screenshot" /><figcaption>Latest screenshot · {formatTime(artifact.created_at)}</figcaption></figure> : null
}

function DrawerSection({ title, children }: { title: string; children: React.ReactNode }) {
  return <section className="v3-drawer-section"><h3>{title}</h3>{children}</section>
}

export function ContextDrawer({
  kind,
  job,
  diff,
  runtimeOwner,
  computer,
  nodes,
  system,
  token,
  busy,
  onClose,
  onAction,
}: {
  kind: DrawerKind | null
  job?: Job
  diff: string
  runtimeOwner: RuntimeOwner
  computer: ComputerStatus | null
  nodes: NodeInfo[]
  system: SystemStatus | null
  token: string
  busy: boolean
  onClose: () => void
  onAction: (path: string, body?: object) => Promise<void>
}) {
  if (!kind) return null
  const title = kind === 'changes' ? 'Changes' : kind === 'details' ? 'Task details' : kind === 'computer' ? 'Computer' : 'System'
  const submitChangesAction = (path: string) => {
    onClose()
    void onAction(path)
  }
  return <div className="v3-drawer-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) onClose() }}>
    <aside className="v3-drawer" role="dialog" aria-modal="true" aria-label={title}>
      <header className="v3-drawer-header"><div><span className="v3-kicker">CONTEXT</span><h2>{title}</h2></div><button className="v3-close" onClick={onClose} aria-label="Close">×</button></header>
      <div className="v3-drawer-body">
        {kind === 'changes' && <>
          <DrawerSection title="Changed files">
            <div className="v3-file-list">{job?.changed_files?.map(file => <div key={file.path}><span>{file.status || 'M'}</span><code>{file.path}</code></div>)}{!job?.changed_files?.length && <p className="v3-muted">No changed files.</p>}</div>
          </DrawerSection>
          <DrawerSection title="Unified diff"><pre className="v3-diff">{diff || 'No diff available.'}</pre></DrawerSection>
          {job && <div className="v3-drawer-actions"><button className="v3-danger" disabled={busy} onClick={() => submitChangesAction(`/api/jobs/${job.id}/discard`)}>Discard…</button><button className="v3-primary" disabled={busy} onClick={() => submitChangesAction(`/api/jobs/${job.id}/apply`)}>Apply…</button></div>}
        </>}
        {kind === 'details' && <>
          <DrawerSection title="Task">
            {job ? <><KeyValue label="ID" value={job.id} mono /><KeyValue label="State" value={job.state} /><KeyValue label="Mode" value={job.mode} /><KeyValue label="Channel" value={job.channel} /><KeyValue label="Started" value={formatTime(job.started_at)} /><KeyValue label="Finished" value={formatTime(job.finished_at)} /><RuntimeOwnerCard owner={runtimeOwner} state={job.state} /></> : <p className="v3-muted">Select a task.</p>}
          </DrawerSection>
          {job?.error && <DrawerSection title="Failure"><pre className="v3-error-detail">{job.error}</pre></DrawerSection>}
          {job && <div className="v3-drawer-actions"><button disabled={busy || !['queued','running'].includes(job.state)} onClick={() => void onAction(`/api/jobs/${job.id}/cancel`)}>Cancel task</button></div>}
        </>}
        {kind === 'computer' && <>
          <DrawerSection title="Computer use"><KeyValue label="CUA" value={computer?.armed ? `Armed · ${computer.arm_remaining_seconds}s` : 'Disarmed'} />{computer?.active_task && <KeyValue label="Active task" value={String(computer.active_task.status || computer.active_task.task_id || 'active')} />}</DrawerSection>
          {computer?.screenshots[0] && <AuthenticatedImage artifact={computer.screenshots[0]} token={token} />}
          <DrawerSection title="Execution nodes"><div className="v3-node-list">{nodes.map(node => <div className="v3-node-row" key={node.id}><span className={`v3-node-dot ${node.status}`} /><span><strong>{node.name}</strong><small>{node.type} · {node.status} · {formatTime(node.last_seen_at)}</small></span></div>)}{!nodes.length && <p className="v3-muted">No execution nodes.</p>}</div></DrawerSection>
          <button className="v3-emergency" onClick={() => void onAction('/api/computer/stop')}>■ Emergency stop</button>
        </>}
        {kind === 'system' && <>
          <DrawerSection title="Host"><KeyValue label="Load" value={system?.load_average.slice(0, 2).map(n => n.toFixed(2)).join(' / ') || '—'} /><KeyValue label="CPU" value={system ? `${system.cpu_count} cores` : '—'} /><KeyValue label="Memory free" value={bytes(system?.memory.available ?? null)} /><KeyValue label="Disk free" value={bytes(system?.disk.free ?? null)} /></DrawerSection>
          <DrawerSection title="Channels"><KeyValue label="Telegram" value={system?.channels.telegram?.configured ? 'Configured' : 'Off'} /><KeyValue label="Feishu" value={system?.channels.feishu?.configured ? 'Configured' : 'Off'} /></DrawerSection>
          <DrawerSection title="Queue"><KeyValue label="Depth" value={String(system?.queue.depth ?? 0)} /><KeyValue label="Paused" value={system?.queue.paused ? 'Yes' : 'No'} /></DrawerSection>
        </>}
      </div>
    </aside>
  </div>
}
