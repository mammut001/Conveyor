import type { RuntimeOwner } from '../runtime';

type Props = { owner: RuntimeOwner; state?: string };

export function RuntimeOwnerCard({ owner, state }: Props) {
  const active = state === 'running';
  const role = owner.id?.split(':', 1)[0] || null;
  return (
    <div className="runtime-owner-card">
      <div>
        <strong>Execution owner</strong>
        <span>{role ? `${role} process` : (active ? 'Binding…' : '—')}</span>
      </div>
      {owner.boundAt && <small>Bound {new Date(owner.boundAt).toLocaleString()}</small>}
    </div>
  );
}
