export type JobEvent = {
  schema_version: number; event_id: string; sequence: number; timestamp: string
  kind: string; job_id: string; payload: Record<string, unknown>; tool_call_id?: string
}

// A replay response can arrive after newer SSE events. Preserve both sources,
// keeping immutable events once and ordered by their durable sequence.
export function mergeJobEvents(current: JobEvent[], incoming: JobEvent[]): JobEvent[] {
  const merged = new Map(current.map(event => [event.event_id, event]))
  for (const event of incoming) {
    if (!merged.has(event.event_id)) merged.set(event.event_id, event)
  }
  return [...merged.values()].sort((a, b) => a.sequence - b.sequence).slice(-1000)
}
