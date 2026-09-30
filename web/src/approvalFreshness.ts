// A refresh that started before a newer one must not paint over it.
// Late polls were putting a decided approval back to "pending" until the
// next interval.

export function isStale(started: number, latest: number): boolean {
  return started !== latest
}

export type InboxApproval = { id: string; status: string; expires_at?: string }

export type InboxLike = {
  approval?: InboxApproval | null
}

export function applyInboxDecision<T extends InboxLike>(
  items: T[],
  approvalId: string,
  status: string,
): T[] {
  return items.map((item) => {
    const appr = item.approval
    if (!appr || appr.id !== approvalId) return item
    return { ...item, approval: { ...appr, status } }
  })
}

export function dropApproval<T extends { id: string }>(items: T[], approvalId: string): T[] {
  return items.filter((item) => item.id !== approvalId)
}

export function shouldRefreshForEvent(kind: string): boolean {
  return (
    kind.startsWith('assistant.')
    || kind.startsWith('task.')
    || kind.startsWith('refinement.')
    || kind.startsWith('approval.')
    || kind.startsWith('apply.')
    || kind.startsWith('discard.')
  )
}
