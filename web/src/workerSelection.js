/** Per-agent canonical Workers session. Shared by Chat, Tasks, and approvals. */

export function parseWorkerSessionMap(raw) {
  try {
    const parsed = JSON.parse(raw || '{}')
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
    const out = {}
    for (const [key, value] of Object.entries(parsed)) {
      if (typeof key === 'string' && typeof value === 'string' && value) out[key] = value
    }
    return out
  } catch {
    return {}
  }
}

/** Remembered session when it still exists, otherwise the agent's main session.

A pending id is the session just created, before the catalog refresh includes it.
*/
export function canonicalWorkerSessionId(agent, picks, pendingId = '') {
  if (!agent) return ''
  const sessions = Array.isArray(agent.sessions) ? agent.sessions : []
  const remembered = sessions.find(session => session && session.id === picks[agent.id])
  if (remembered) return remembered.id
  if (pendingId && picks[agent.id] === pendingId) return pendingId
  const main = sessions.find(session => session && session.kind === 'main')
  if (main) return main.id
  if (sessions[0] && sessions[0].id) return sessions[0].id
  return agent.session_id || ''
}
