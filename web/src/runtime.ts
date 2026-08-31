export type RuntimeOwner = {
  id: string | null;
  boundAt?: string | null;
};

export type TranscriptMessage = {
  id: string;
  session_id: string;
  job_id?: string | null;
  role: 'user' | 'assistant' | 'system' | 'tool';
  kind?: string | null;
  content: string;
  created_at: string;
  metadata?: Record<string, unknown>;
};

export type SessionTranscript = {
  id: string;
  channel?: string | null;
  operator_id?: string | null;
  title: string;
  created_at: string;
  updated_at: string;
  archived: number;
  messages?: TranscriptMessage[];
};

export function runtimeOwnerFromJob(job: any): RuntimeOwner {
  const metadata = job?.metadata ?? {};
  return {
    id: typeof metadata.execution_owner_id === 'string' ? metadata.execution_owner_id : null,
    boundAt:
      typeof metadata.execution_owner_bound_at === 'string'
        ? metadata.execution_owner_bound_at
        : null,
  };
}

export function terminalJobState(state?: string | null): boolean {
  return ['completed', 'failed', 'cancelled', 'interrupted'].includes(state ?? '');
}
