import type { ServerEvent } from './types';

const KEY = 'agent8s-token';

// The token arrives in the URL fragment (never sent to the server or logged);
// keep it for reloads and scrub it from the address bar.
function readToken(): string {
  const match = location.hash.match(/token=([^&]+)/);
  if (match) {
    try { sessionStorage.setItem(KEY, match[1]); } catch { /* storage may be blocked */ }
    history.replaceState(null, '', location.pathname + location.search);
    return match[1];
  }
  try { return sessionStorage.getItem(KEY) ?? ''; } catch { return ''; }
}

export const token = readToken();

export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: { 'X-Agent8s-Token': token, ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}) },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(data.error ?? `Ошибка ${res.status}`, res.status);
  return data as T;
}

export const api = {
  get: <T>(path: string) => request<T>('GET', path),
  post: <T>(path: string, body: unknown = {}) => request<T>('POST', path, body),
  patch: <T>(path: string, body: unknown) => request<T>('PATCH', path, body),
  del: <T>(path: string) => request<T>('DELETE', path),
};

export function connect(onEvent: (e: ServerEvent) => void, onOpen: () => void, onClose: () => void): () => void {
  let ws: WebSocket | null = null;
  let delay = 400;
  let stopped = false;
  let timer: number | undefined;

  const open = () => {
    ws = new WebSocket(`ws://${location.host}/ws?token=${encodeURIComponent(token)}`);
    ws.onopen = () => { delay = 400; onOpen(); };
    ws.onmessage = (m) => {
      try { onEvent(JSON.parse(m.data)); } catch { /* ignore malformed frame */ }
    };
    ws.onclose = () => {
      onClose();
      if (!stopped) timer = window.setTimeout(open, delay = Math.min(delay * 1.7, 5000));
    };
  };
  open();
  return () => { stopped = true; window.clearTimeout(timer); ws?.close(); };
}

interface NativeApi { pick_folder(): Promise<string | null>; open_url(url: string): Promise<void> }
export const native = (): NativeApi | null => (window as unknown as { pywebview?: { api: NativeApi } }).pywebview?.api ?? null;

export function openExternal(url: string) {
  const n = native();
  if (n) void n.open_url(url);
  else window.open(url, '_blank', 'noopener,noreferrer');
}
