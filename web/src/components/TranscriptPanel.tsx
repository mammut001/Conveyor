import type { TranscriptMessage } from '../runtime';
import { FormattedText } from './FormattedText';

type Props = { messages: TranscriptMessage[] };

export function TranscriptPanel({ messages }: Props) {
  if (!messages.length) return <div className="empty-state">No transcript yet.</div>;

  return (
    <div className="transcript-panel" aria-live="polite">
      {messages.map((message) => {
        const isError = message.content.startsWith('[error]');
        const cleanContent = isError
          ? message.content.replace(/^\[error\]\s*/i, '').trim()
          : message.content;

        if (isError) {
          return (
            <div key={message.id} className="transcript-error-card">
              <span className="error-icon" aria-hidden="true">⚠️</span>
              <div className="error-body">
                <span className="error-title">执行异常</span>
                <p className="error-message">{cleanContent}</p>
              </div>
            </div>
          );
        }

        if (message.role === 'user') {
          return (
            <article key={message.id} className="transcript-message role-user">
              <div className="transcript-content">
                <FormattedText content={cleanContent} />
              </div>
            </article>
          );
        }

        return (
          <article key={message.id} className={`transcript-message role-${message.role}`}>
            <div className="transcript-avatar" aria-hidden="true">
              {message.role === 'assistant' ? '🤖' : message.role === 'tool' ? '⌘' : '⚙'}
            </div>
            <div className="transcript-body">
              <div className="transcript-content">
                <FormattedText content={cleanContent} />
              </div>
              <footer className="transcript-footer">
                <time dateTime={message.created_at}>
                  {new Date(message.created_at).toLocaleString([], {
                    month: 'short',
                    day: 'numeric',
                    hour: '2-digit',
                    minute: '2-digit',
                  })}
                </time>
              </footer>
            </div>
          </article>
        );
      })}
    </div>
  );
}
