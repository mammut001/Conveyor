import type { TranscriptMessage } from '../runtime';
import { FormattedText } from './FormattedText';
import { formatChatTime, formatFullTime } from './chatFormat';

type Props = {
  messages: TranscriptMessage[];
  agent?: { name: string; color?: string };
  /** 重试 on a failure line: resend the question that was asked just before it. */
  onRetry?: (question: string) => void;
};

/** A provider failure is stored with kind="error" (web_console WebOutbound.note_provider_failure). */
function isFailureMessage(message: TranscriptMessage): boolean {
  return message.kind === 'error' || message.content.startsWith('[error]');
}

function failureText(message: TranscriptMessage): string {
  return message.content.replace(/^\[error\]\s*/i, '').trim();
}

export function TranscriptPanel({ messages, agent, onRetry }: Props) {
  const agentName = agent?.name?.trim() || 'Conveyor';
  if (!messages.length) return <div className="empty-state">还没有对话记录。</div>;

  return (
    <div className="transcript-panel" aria-live="polite">
      {messages.map((message, index) => {
        const isError = isFailureMessage(message);
        const cleanContent = isError
          ? failureText(message)
          : message.content;

        if (isError) {
          // The question that failed: the closest user message above this line. 重试 sends it again.
          let question = '';
          for (let i = index - 1; i >= 0; i -= 1) {
            if (messages[i].role === 'user') { question = messages[i].content.trim(); break }
          }
          return (
            <div key={message.id} className="transcript-error-card" role="alert">
              <span className="error-icon" aria-hidden="true">⚠️</span>
              <div className="error-body">
                <span className="error-title">发送没有成功</span>
                <p className="error-message">{cleanContent}</p>
                {onRetry && question && (
                  <button type="button" className="transcript-error-retry" onClick={() => onRetry(question)}>
                    重试
                  </button>
                )}
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
