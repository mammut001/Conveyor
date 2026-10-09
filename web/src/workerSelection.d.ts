/** Per-agent canonical Workers session. Types for workerSelection.js. */

export function parseWorkerSessionMap(raw: string | null): Record<string, string>

export function canonicalWorkerSessionId(
  agent: {
    id: string
    session_id?: string
    sessions?: Array<{ id?: string; kind?: string } | null>
  } | null | undefined,
  picks: Record<string, string>,
  pendingId?: string,
): string
