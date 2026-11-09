import { useState } from 'react';
import { NotesEditor } from './NotesEditor';

/**
 * 「项目笔记」 card on the 项目 page: the agent's notes for one repository
 * (scope "repo:<repo_id>"). Collapsed by default; the notes load when it opens.
 */
export function ProjectNotesCard({ token, repoId, repoName }: { token: string; repoId: string; repoName?: string }) {
  const [open, setOpen] = useState(false);
  const [count, setCount] = useState<number | null>(null);

  return (
    <details
      className="sup-notes"
      open={open}
      onToggle={(event) => setOpen((event.currentTarget as HTMLDetailsElement).open)}
    >
      <summary className="sup-notes-summary">
        <span className="sup-notes-chevron" aria-hidden="true">▸</span>
        <span>项目笔记{count !== null ? `（${count} 条）` : ''}</span>
        <span className="sup-notes-summary-hint">点开可以查看和修改</span>
      </summary>
      {open && (
        <div className="sup-notes-body" role="region" aria-label={repoName ? `${repoName} 的项目笔记` : '项目笔记'}>
          <NotesEditor
            token={token}
            scope={`repo:${repoId}`}
            label="项目笔记"
            explain="分析、动手改、检查时都会参考这些笔记；目标达成后它也会自己补充经验。"
            placeholder="例如：改完页面要在手机宽度下看一遍"
            onCountChange={setCount}
          />
        </div>
      )}
    </details>
  );
}

export default ProjectNotesCard;
