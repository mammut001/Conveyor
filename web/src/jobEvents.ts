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

/**
 * The visible text of a running turn, from its ordered `assistant.delta` texts.
 *
 * Runners do not agree on what a delta carries: Claude Code streams the new
 * characters, while Pi (through the queue's progress callback) re-sends the
 * whole answer so far. Both can reach the same job, so a plain join repeats the
 * lead sentence inside the middle of the reply. A text that already starts with
 * everything received so far is a snapshot: it replaces the accumulation instead
 * of being appended to it.
 */
export function streamedText(deltas: string[]): string {
  let text = ''
  for (const delta of deltas) {
    if (!delta) continue
    if (text && delta.startsWith(text)) text = delta
    else text += delta
  }
  return text
}
