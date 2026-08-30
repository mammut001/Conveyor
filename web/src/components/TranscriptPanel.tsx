import type { TranscriptMessage } from '../runtime';

type Props = { messages: TranscriptMessage[] };

function labelForRole(role: TranscriptMessage['role']): string {
  if (role === 'user') return 'You';
  if (role === 'assistant') return 'Conveyor';
  if (role === 'tool') return 'Tool';
  return 'System';
}

export function TranscriptPanel({ messages }: Props) {
  if (!messages.length) return <div className="empty-state">No transcript yet.</div>;
  return (
    <div className="transcript-panel" aria-live="polite">
      {messages.map((message) => (
        <article key={message.id} className={`transcript-message role-${message.role}`}>
          <header>
            <strong>{labelForRole(message.role)}</strong>
            <time dateTime={message.created_at}>{new Date(message.created_at).toLocaleString()}</time>
          </header>
          <pre>{message.content}</pre>
        </article>
      ))}
    </div>
  );
}
