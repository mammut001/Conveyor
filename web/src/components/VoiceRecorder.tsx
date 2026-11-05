import { useRef, useState } from 'react';

type Props = { disabled: boolean; onAudio: (audio: Blob) => void; onError: (error: string) => void };

export function VoiceRecorder({ disabled, onAudio, onError }: Props) {
  const [recording, setRecording] = useState(false);
  const active = useRef<{ recorder: MediaRecorder; stream: MediaStream; timeout: number } | null>(null);
  const toggle = async () => {
    if (active.current) {
      active.current.recorder.stop();
      return;
    }
    if (disabled || !navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      onError('这个浏览器不支持录音，请改用文字输入。');
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (disabled) { stream.getTracks().forEach(t => t.stop()); return; }
      const mimeType = ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/mp4']
        .find(type => MediaRecorder.isTypeSupported(type));
      if (!mimeType) {
        stream.getTracks().forEach(t => t.stop());
        onError('这个浏览器不支持录音格式，请改用文字输入。');
        return;
      }
      const recorder = new MediaRecorder(stream, { mimeType });
      const chunks: Blob[] = [];
      recorder.ondataavailable = event => { if (event.data.size) chunks.push(event.data); };
      recorder.onerror = () => onError('录音失败，请再试一次。');
      recorder.onstop = () => {
        window.clearTimeout(active.current?.timeout);
        stream.getTracks().forEach(t => t.stop());
        active.current = null;
        setRecording(false);
        const audio = new Blob(chunks, { type: mimeType });
        if (audio.size > 0) onAudio(audio);
        else onError('没有录到声音，请再试一次。');
      };
      recorder.start();
      active.current = {
        recorder, stream,
        timeout: window.setTimeout(() => { if (recorder.state === 'recording') recorder.stop(); }, 120_000),
      };
      setRecording(true);
    } catch {
      onError('无法使用麦克风，请在浏览器里允许麦克风权限。');
    }
  };
  const label = recording ? '停止录音' : '语音输入';
  return (
    <button type="button" className={`chat-voice-button ${recording ? 'is-recording' : ''}`}
      onClick={toggle} disabled={disabled && !recording}
      aria-label={label} title={recording ? '停止录音并发送' : '按一下开始录音，再按一下发送'}>
      {recording
        ? <><span className="chat-voice-dot" aria-hidden="true" /><span aria-hidden="true">录音中</span></>
        : <svg aria-hidden="true" viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
            <rect x="7" y="2.5" width="6" height="10" rx="3" />
            <path d="M4.5 9.5a5.5 5.5 0 0 0 11 0M10 15v2.5" />
          </svg>}
    </button>
  );
}
