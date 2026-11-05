import { useEffect, useRef, useState } from 'react';

// The console is often opened over plain HTTP (a tailnet host), where
// navigator.clipboard does not exist; fall back to a hidden selection copy.
async function copyText(text: string, origin: HTMLElement | null): Promise<void> {
  if (navigator.clipboard?.writeText) {
    try { await navigator.clipboard.writeText(text); return; } catch { /* try the fallback */ }
  }
  const area = document.createElement('textarea');
  area.value = text;
  area.setAttribute('readonly', '');
  area.style.position = 'fixed';
  area.style.top = '-1000px';
  area.style.opacity = '0';
  document.body.appendChild(area);
  area.select();
  let copied = false;
  try { copied = document.execCommand('copy'); } catch { copied = false; }
  area.remove();
  origin?.focus({ preventScroll: true });
  if (!copied) throw new Error('copy failed');
}

type Props = { text: string; className?: string; label?: string; title?: string };

export function CopyButton({ text, className = '', label = '复制', title = '复制到剪贴板' }: Props) {
  const [status, setStatus] = useState<'idle' | 'copied' | 'failed'>('idle');
  const timerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const buttonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    setStatus('idle');
    return () => clearTimeout(timerRef.current);
  }, [text]);

  const handleCopy = async () => {
    clearTimeout(timerRef.current);
    try {
      await copyText(text, buttonRef.current);
      setStatus('copied');
    } catch {
      setStatus('failed');
    }
    timerRef.current = setTimeout(() => setStatus('idle'), 2000);
  };

  return (
    <button ref={buttonRef} type="button" className={`copy-button ${className} ${status !== 'idle' ? `is-${status}` : ''}`}
      onClick={handleCopy} title={title} aria-live="polite">
      <svg aria-hidden="true" viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        {status === 'copied'
          ? <path d="M3.5 8.5l3 3 6-7" />
          : <><rect x="5.5" y="5.5" width="8" height="8" rx="1.5" /><path d="M10.5 3.5v-.5a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h.5" /></>}
      </svg>
      <span>{status === 'copied' ? '已复制' : status === 'failed' ? '复制失败' : label}</span>
    </button>
  );
}
