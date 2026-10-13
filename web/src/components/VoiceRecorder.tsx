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
      onError('Microphone recording is unavailable in this browser.');
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (disabled) { stream.getTracks().forEach(t => t.stop()); return; }
      const mimeType = ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/mp4']
        .find(type => MediaRecorder.isTypeSupported(type));
      if (!mimeType) {
        stream.getTracks().forEach(t => t.stop());
        onError('No supported audio recorder format.');
        return;
      }
      const recorder = new MediaRecorder(stream, { mimeType });
      const chunks: Blob[] = [];
      recorder.ondataavailable = event => { if (event.data.size) chunks.push(event.data); };
      recorder.onerror = () => onError('Microphone recording failed.');
      recorder.onstop = () => {
        window.clearTimeout(active.current?.timeout);
        stream.getTracks().forEach(t => t.stop());
        active.current = null;
        setRecording(false);
        const audio = new Blob(chunks, { type: mimeType });
        if (audio.size > 0) onAudio(audio);
        else onError('Empty audio recording.');
      };
      recorder.start();
      active.current = {
        recorder, stream,
        timeout: window.setTimeout(() => { if (recorder.state === 'recording') recorder.stop(); }, 120_000),
      };
      setRecording(true);
    } catch {
      onError('Microphone permission denied or unavailable.');
    }
  };
  return (
    <button type="button" className="mode-switch" style={{ alignSelf: 'center', cursor: 'pointer' }}
      onClick={toggle} disabled={disabled && !recording}
      aria-label={recording ? 'Stop recording' : 'Record voice message'}
      title={recording ? 'Stop recording' : 'Record voice message'}>
      {recording ? '⏹ Stop' : '🎤'}
    </button>
  );
}
