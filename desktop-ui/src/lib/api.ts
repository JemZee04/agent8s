import { ApiError } from './errors';
import type { RelayTransport } from './relay';
import type { ServerEvent } from './types';

export { ApiError };

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

// Same bundle serves the desktop window and the phone; the relay marks its copy of index.html.
export const relayMode = document.querySelector('meta[name="agent8s-mode"]')?.getAttribute('content') === 'relay';

let relay: RelayTransport | null = null;
export const setRelay = (transport: RelayTransport | null) => { relay = transport; };

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  if (relayMode) {
    if (!relay) throw new ApiError('Нет подключения к компьютеру.', 503);
    return relay.request<T>(method, path, body);
  }
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
