import { Component, lazy, memo, Suspense, type ReactNode } from 'react';

const MarkdownContent = lazy(() => import('./MarkdownContent'));

type Props = {
  content: string;
  className?: string;
};

// A failed optional chunk should leave the reply readable, including offline.
class MarkdownBoundary extends Component<{ children: ReactNode; fallback: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() { return this.state.failed ? <div data-render-mode="plain-text">{this.props.fallback}</div> : this.props.children; }
}

// Keep the editor and already-rendered turns responsive while a new answer streams.
export const FormattedText = memo(function FormattedText({ content, className = '' }: Props) {
  if (!content) return null;
  return (
    <div className={`formatted-text-flow ${className}`}>
      <MarkdownBoundary fallback={<p className="formatted-paragraph">{content}</p>}>
        <Suspense fallback={<p className="formatted-paragraph">{content}</p>}>
          <MarkdownContent content={content} />
        </Suspense>
      </MarkdownBoundary>
    </div>
  );
});
