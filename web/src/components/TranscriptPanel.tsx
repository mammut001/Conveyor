import type { TranscriptMessage } from '../runtime';
import { FormattedText } from './FormattedText';

type Props = { messages: TranscriptMessage[] };

function labelForRole(role: TranscriptMessage['role']): string {
  if (role === 'user') return 'You';
  if (role === 'assistant') return 'Conveyor';
  if (role === 'tool') return 'Tool';
  return 'System';
}

function avatarForRole(role: TranscriptMessage['role']): string {
  if (role === 'user') return '👤';
  if (role === 'assistant') return '🤖';
  if (role === 'tool') return '⌘';
  return '⚙';
}

export function TranscriptPanel({ messages }: Props) {
  if (!messages.length) return <div className="empty-state">No transcript yet.</div>;
  return (
    <div className="transcript-panel" aria-live="polite">
      {messages.map((message) => (
        <article key={message.id} className={`transcript-message role-${message.role}`}>
          <div className="transcript-avatar" aria-hidden="true">
            {avatarForRole(message.role)}
          </div>
          <div className="transcript-body">
            <header className="transcript-header">
              <span className="transcript-sender">{labelForRole(message.role)}</span>
              <time dateTime={message.created_at}>
                {new Date(message.created_at).toLocaleString([], {
                  month: 'short',
                  day: 'numeric',
                  hour: '2-digit',
                  minute: '2-digit',
                })}
              </time>
            </header>
            <div className="transcript-content">
              <FormattedText content={message.content} />
            </div>
          </div>
        </article>
      ))}
    </div>
  );
}
