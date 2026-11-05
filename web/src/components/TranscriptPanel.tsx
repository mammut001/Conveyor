import type { TranscriptMessage } from '../runtime';
import { FormattedText } from './FormattedText';
import { formatChatTime, formatFullTime } from './chatFormat';

type Props = { messages: TranscriptMessage[]; agent?: { name: string; color?: string } };

export function TranscriptPanel({ messages, agent }: Props) {
  const agentName = agent?.name?.trim() || 'Conveyor';
  if (!messages.length) return <div className="empty-state">还没有对话记录。</div>;

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
            {/* Replies wear the agent's letter avatar, the same as in 对话. */}
            {message.role === 'assistant'
              ? <div className="transcript-avatar chat-avatar" aria-hidden="true" style={agent?.color ? { background: agent.color } : undefined}>
                  {(Array.from(agentName)[0] || 'C').toUpperCase()}
                </div>
              : <div className="transcript-avatar" aria-hidden="true">{message.role === 'tool' ? '⌘' : '⚙'}</div>}
            <div className="transcript-body">
              <div className="transcript-content">
                <FormattedText content={cleanContent} />
              </div>
              <footer className="transcript-footer">
                <time dateTime={message.created_at} title={formatFullTime(message.created_at)}>
                  {formatChatTime(message.created_at)}
                </time>
              </footer>
            </div>
          </article>
        );
      })}
    </div>
  );
}
