import React, { useState } from 'react';

type Props = {
  content: string;
  className?: string;
};

function CopyButton({ code }: { code: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      /* ignore */
    }
  };

  return (
    <button
      type="button"
      className="code-copy-btn"
      onClick={handleCopy}
      title="Copy code to clipboard"
    >
      {copied ? '✓ Copied' : 'Copy'}
    </button>
  );
}

function renderInline(text: string): React.ReactNode[] {
  // Regex to match inline code (`code`), bold (**bold**), and links ([title](url))
  const regex = /(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\([^)]+\))/g;
  const parts = text.split(regex);

  return parts.map((part, index) => {
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
      return (
        <code key={index} className="inline-code">
          {part.slice(1, -1)}
        </code>
      );
    }
    if (part.startsWith('**') && part.endsWith('**') && part.length > 4) {
      return <strong key={index}>{part.slice(2, -2)}</strong>;
    }
    const linkMatch = part.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
    if (linkMatch) {
      return (
        <a
          key={index}
          href={linkMatch[2]}
          target="_blank"
          rel="noopener noreferrer"
          className="text-link"
        >
          {linkMatch[1]}
        </a>
      );
    }
    return part;
  });
}

export function FormattedText({ content, className = '' }: Props) {
  if (!content) return null;

  // Split by code blocks: ```[lang]\n[code]\n```
  const codeBlockRegex = /```([a-zA-Z0-9_-]*)\n([\s\S]*?)```/g;
  const elements: React.ReactNode[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  let keyCounter = 0;

  while ((match = codeBlockRegex.exec(content)) !== null) {
    const textBefore = content.slice(lastIndex, match.index);
    if (textBefore.trim()) {
      // Process paragraphs in textBefore
      const paragraphs = textBefore.split(/\n\s*\n/);
      paragraphs.forEach((para) => {
        const trimmed = para.trim();
        if (!trimmed) return;
        // Check for bullet list
        const lines = trimmed.split('\n');
        const isList = lines.every((l) => /^\s*[-*•]\s+/.test(l));
        if (isList) {
          elements.push(
            <ul key={`ul-${keyCounter++}`} className="formatted-list">
              {lines.map((l, lIdx) => (
                <li key={lIdx}>{renderInline(l.replace(/^\s*[-*•]\s+/, ''))}</li>
              ))}
            </ul>
          );
        } else {
          elements.push(
            <p key={`p-${keyCounter++}`} className="formatted-paragraph">
              {lines.map((line, lIdx) => (
                <React.Fragment key={lIdx}>
                  {lIdx > 0 && <br />}
                  {renderInline(line)}
                </React.Fragment>
              ))}
            </p>
          );
        }
      });
    }

    const lang = match[1] || 'text';
    const code = match[2].trimEnd();
    elements.push(
      <div key={`code-${keyCounter++}`} className="code-block-container">
        <div className="code-block-header">
          <span className="code-lang-tag">{lang}</span>
          <CopyButton code={code} />
        </div>
        <pre className="code-block-pre">
          <code>{code}</code>
        </pre>
      </div>
    );

    lastIndex = match.index + match[0].length;
  }

  // Trailing text after the last code block
  const remainingText = content.slice(lastIndex);
  if (remainingText.trim()) {
    const paragraphs = remainingText.split(/\n\s*\n/);
    paragraphs.forEach((para) => {
      const trimmed = para.trim();
      if (!trimmed) return;
      const lines = trimmed.split('\n');
      const isList = lines.every((l) => /^\s*[-*•]\s+/.test(l));
      if (isList) {
        elements.push(
          <ul key={`ul-${keyCounter++}`} className="formatted-list">
            {lines.map((l, lIdx) => (
              <li key={lIdx}>{renderInline(l.replace(/^\s*[-*•]\s+/, ''))}</li>
            ))}
          </ul>
        );
      } else {
        elements.push(
          <p key={`p-${keyCounter++}`} className="formatted-paragraph">
            {lines.map((line, lIdx) => (
              <React.Fragment key={lIdx}>
                {lIdx > 0 && <br />}
                {renderInline(line)}
              </React.Fragment>
            ))}
          </p>
        );
      }
    });
  }

  return <div className={`formatted-text-flow ${className}`}>{elements}</div>;
}
