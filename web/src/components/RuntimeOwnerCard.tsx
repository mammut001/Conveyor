import type { RuntimeOwner } from '../runtime';

type Props = { owner: RuntimeOwner; state?: string };

const ROLES: Record<string, string> = { web: '网页', telegram: 'Telegram', feishu: '飞书' };

export function RuntimeOwnerCard({ owner, state }: Props) {
  const active = state === 'running';
  const role = owner.id?.split(':', 1)[0] || null;
  const bound = owner.boundAt ? new Date(owner.boundAt) : null;
  return (
    <div className="runtime-owner-card">
      <div>
        <strong>执行进程</strong>
        <span>{role ? (ROLES[role] || role) : (active ? '正在分配…' : '—')}</span>
      </div>
      {bound && !Number.isNaN(bound.valueOf()) && <small>接手于 {bound.toLocaleString('zh-CN', { month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false })}</small>}
    </div>
  );
}
