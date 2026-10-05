export type Part =
  | { type: 'text' | 'thinking'; text: string }
  | { type: 'tool'; id: string; name: string; input: unknown; output: string | null; is_error: boolean; status: 'running' | 'done' }
  | { type: 'usage'; cost_usd?: number; duration_ms?: number; input_tokens?: number; output_tokens?: number }
  | { type: 'error'; text: string };

export interface Message {
  id: number;
  chat_id: number;
  role: 'user' | 'assistant' | 'system';
  agent: string | null;
  model: string | null;
  parts: Part[];
  status: 'done' | 'running' | 'error' | 'interrupted';
  created_at: string;
  rev?: number;
}

export interface Chat {
  id: number;
  project_id: number;
  project_name: string;
  title: string;
  mode: 'worktree' | 'direct';
  branch: string;
  worktree_path: string;
  agent: string;
  model: string;
  effort: string;
  status: 'idle' | 'running' | 'error';
  changes: number;
  running: boolean;
  running_since: number | null;
  default_branch: string;
  sessions: string[];
  extra_dirs: string[];
  created_at: string;
  updated_at: string;
}

export interface ModelSpec { id: string; label: string; efforts: string[] }
export interface AgentSpec { id: string; label: string; models: ModelSpec[] }
export interface Project { id: number; name: string; path: string; default_branch: string }

export type Op =
  | { op: 'append'; i: number; text: string }
  | { op: 'set'; i: number; part: Part };

export type ServerEvent =
  | { t: 'chat'; chat: Chat }
  | { t: 'chat_deleted'; chat_id: number }
  | { t: 'msg_new'; chat_id: number; message: Message }
  | { t: 'msg_ops'; chat_id: number; msg_id: number; rev: number; ops: Op[] }
  | { t: 'msg_status'; chat_id: number; msg_id: number; status: Message['status'] }
  | { t: 'diff_changed'; chat_id: number }
  | { t: 'preview'; op: 'add' | 'del'; cap: string; port: number; chat_id: number; exp: number; kind: 'port' | 'file'; name: string; url?: string };
