import type { RuntimeOwner } from '../runtime';

type Props = { owner: RuntimeOwner; state?: string };

export function RuntimeOwnerCard({ owner, state }: Props) {
  const active = state === 'running';
  return (
    <div className="runtime-owner-card">
      <div>
        <strong>Execution owner</strong>
        <span>{owner.id ?? (active ? 'Binding…' : '—')}</span>
      </div>
      {owner.boundAt && <small>Bound {new Date(owner.boundAt).toLocaleString()}</small>}
    </div>
  );
}
