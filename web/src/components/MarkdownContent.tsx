import { isValidElement, type ReactNode } from 'react';
import Markdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { CopyButton } from './CopyButton';

function CodeBlock({ children }: { children?: ReactNode }) {
  const child = isValidElement<{ children?: ReactNode; className?: string }>(children) ? children : null;
  const raw = typeof child?.props.children === 'string' ? child.props.children : '';
  // The markdown renderer adds one terminal newline; retain all code indentation.
  const code = raw.replace(/\n$/, '');
  const language = /(?:^|\s)language-([^\s]+)/.exec(child?.props.className || '')?.[1] || '';
  return (
    <div className="code-block-container">
      <div className="code-block-header">
        <span className="code-lang-tag">{language && language !== 'text' ? language : '代码'}</span>
        <CopyButton text={code} className="code-copy-btn" title="复制代码" />
      </div>
      <pre className="code-block-pre"><code>{code}</code></pre>
    </div>
  );
}

const components: Components = {
  p: ({ children }) => <p className="formatted-paragraph">{children}</p>,
  ul: ({ children, className }) => <ul className={`formatted-list ${className || ''}`}>{children}</ul>,
  ol: ({ children, start }) => <ol className="formatted-list" start={start}>{children}</ol>,
  blockquote: ({ children }) => <blockquote className="formatted-quote">{children}</blockquote>,
  code: ({ children, className }) => <code className={className || 'inline-code'}>{children}</code>,
  pre: CodeBlock,
  table: ({ children }) => (
    <div className="formatted-table-scroll" role="region" aria-label="表格" tabIndex={0}>
      <table className="formatted-table">{children}</table>
    </div>
  ),
  a: ({ href, children }) => !href ? <span>{children}</span> : (
    <a className="text-link" href={href}
      target={href.startsWith('#') ? undefined : '_blank'} rel="noopener noreferrer">{children}</a>
  ),
  // Keep referenced images explicit links, as in the existing text-only renderer.
  img: ({ src, alt }) => src ? <a className="text-link" href={src} target="_blank" rel="noopener noreferrer">{alt || '打开图片'}</a> : <span>{alt}</span>,
};
const remarkPlugins = [remarkGfm];

export default function MarkdownContent({ content }: { content: string }) {
  // HTML remains escaped, and the renderer's default URL sanitization stays on.
  return <Markdown remarkPlugins={remarkPlugins} components={components}>{content}</Markdown>;
}
