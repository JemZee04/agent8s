// Phone transport: the same API as the desktop window, tunnelled through the
// relay inside end-to-end encrypted frames (protocol: see remote.py).
import { ApiError } from './errors';
import type { ServerEvent } from './types';
import { C2H, H2C, b64urlDecode, deriveKeys, open, randomHex, seal, type Keys } from './wire';

export type LinkState = 'connecting' | 'online' | 'offline';

// -- key storage: the AES key is non-extractable, so page script can use it but never read it --

const DB = 'agent8s';
let memory: Keys | null = null;

function idb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB, 1);
    req.onupgradeneeded = () => req.result.createObjectStore('kv');
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function kv<T>(mode: IDBTransactionMode, run: (store: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  const db = await idb();
  return new Promise((resolve, reject) => {
    const req = run(db.transaction('kv', mode).objectStore('kv'));
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

export async function loadKeys(): Promise<Keys | null> {
  try {
    return ((await kv('readonly', (s) => s.get('keys'))) as Keys | undefined) ?? memory;
  } catch {
    return memory; // storage blocked (private mode): works until the page is closed
  }
}

export async function saveKeys(keys: Keys): Promise<void> {
  memory = keys;
  try { await kv('readwrite', (s) => s.put(keys, 'keys')); } catch { /* keep the in-memory copy */ }
}

export async function forgetKeys(): Promise<void> {
  memory = null;
  try { await kv('readwrite', (s) => s.delete('keys')); } catch { /* nothing stored */ }
}

/** Accepts a full pairing link (…/#k=KEY) or the bare 43-character key. */
export async function pairFromText(text: string): Promise<Keys> {
  const match = text.match(/[#&]k=([A-Za-z0-9_-]{43})(?![A-Za-z0-9_-])/) ?? text.trim().match(/^([A-Za-z0-9_-]{43})$/);
  if (!match) throw new ApiError('Это не похоже на ключ сопряжения. Откройте QR-код в приложении на компьютере.', 400);
  const keys = await deriveKeys(b64urlDecode(match[1]));
  await saveKeys(keys);
  return keys;
}

// -- transport --------------------------------------------------------------------------

interface Callbacks {
  event: (e: ServerEvent) => void;
  open: () => void;
  close: () => void;
  state: (s: LinkState) => void;
}

interface Pending {
  resolve: (v: any) => void;
  reject: (e: Error) => void;
  timer: number;
}

const REQUEST_TIMEOUT = 130_000;
const HANDSHAKE_TIMEOUT = 10_000;

export class RelayTransport {
  private ws: WebSocket | null = null;
  private sn = '';
  private cn = '';
  private counter = 0;
  private nextId = 0;
  private pending = new Map<number, Pending>();
  private chain: Promise<void> = Promise.resolve();
  private stopped = false;
  private delay = 500;
  private retry: number | undefined;
  private handshakeTimer: number | undefined;
  private state: LinkState = 'connecting';

  constructor(private keys: Keys, private cb: Callbacks) {}

  start() {
    document.addEventListener('visibilitychange', this.wake);
    window.addEventListener('online', this.wake);
    this.connect();
  }

  stop() {
    this.stopped = true;
    document.removeEventListener('visibilitychange', this.wake);
    window.removeEventListener('online', this.wake);
    window.clearTimeout(this.retry);
    this.ws?.close();
  }

  // iOS freezes sockets of backgrounded pages; reconnect as soon as we are visible again.
  private wake = () => {
    if (document.visibilityState === 'hidden' || this.stopped) return;
    if (!this.ws || this.ws.readyState > WebSocket.OPEN) {
      window.clearTimeout(this.retry);
      this.delay = 500;
      this.connect();
    }
  };

  private setState(s: LinkState) {
    if (this.state !== s) {
      this.state = s;
      this.cb.state(s);
    }
  }

  private url(): string {
    const base = location.pathname.replace(/[^/]*$/, '');
    return `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}${base}ws/client`;
  }

  private connect() {
    if (this.stopped) return;
    this.setState('connecting');
    const ws = (this.ws = new WebSocket(this.url()));
    ws.onopen = () => ws.send(JSON.stringify({ t: 'hello', room: this.keys.room }));
    ws.onmessage = (m) => void this.onMessage(ws, String(m.data));
    ws.onclose = () => {
      if (this.ws !== ws) return;
      this.sn = '';
      window.clearTimeout(this.handshakeTimer);
      for (const p of this.pending.values()) {
        window.clearTimeout(p.timer);
        p.reject(new ApiError('Нет связи с компьютером.', 503));
      }
      this.pending.clear();
      this.cb.close();
      if (this.state === 'online') this.setState('connecting');
      if (!this.stopped) {
        this.retry = window.setTimeout(() => this.connect(), this.delay);
        this.delay = Math.min(this.delay * 1.8, 10_000);
      }
    };
  }

  private async onMessage(ws: WebSocket, data: string) {
    if (ws !== this.ws) return;
    if (data.startsWith('{')) {
      let control: { t?: string; host?: boolean };
      try { control = JSON.parse(data); } catch { return; }
      if (control.t === 'presence') {
        if (control.host) this.handshake(ws);
        else {
          this.sn = '';
          this.setState('offline');
        }
      }
      return;
    }
    const msg = await open(this.keys, H2C, data);
    if (!msg) return;
    if (msg.k === 'welcome' && msg.cn === this.cn) {
      window.clearTimeout(this.handshakeTimer);
      this.sn = msg.sn;
      this.counter = 0;
      this.delay = 500;
      this.setState('online');
      this.cb.open();
    } else if (msg.k === 'res') {
      const p = this.pending.get(msg.id);
      if (!p) return;
      this.pending.delete(msg.id);
      window.clearTimeout(p.timer);
      if (msg.s === 401 && msg.b?.error === 'stale') {
        this.handshake(ws); // the Mac restarted: our nonce is void
        p.reject(new ApiError('Соединение обновилось, повторите.', 503));
      } else if (msg.s >= 400) p.reject(new ApiError(msg.b?.error ?? `Ошибка ${msg.s}`, msg.s));
      else p.resolve(msg.b);
    } else if (msg.k === 'ev' && Array.isArray(msg.e)) {
      for (const e of msg.e) this.cb.event(e);
    }
  }

  private handshake(ws: WebSocket) {
    this.sn = '';
    this.cn = randomHex(8);
    this.setState('connecting');
    void this.send(ws, { k: 'hello', cn: this.cn });
    window.clearTimeout(this.handshakeTimer);
    // No welcome: the Mac is not answering. Drop the socket and retry from scratch.
    this.handshakeTimer = window.setTimeout(() => ws.close(), HANDSHAKE_TIMEOUT);
  }

  private async send(ws: WebSocket, obj: unknown) {
    const frame = await seal(this.keys, C2H, obj);
    if (ws.readyState === WebSocket.OPEN) ws.send(frame);
  }

  request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const ws = this.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN || !this.sn) {
      return Promise.reject(
        new ApiError(this.state === 'offline' ? 'Компьютер не в сети.' : 'Подключаюсь к компьютеру…', 503),
      );
    }
    const id = ++this.nextId;
    return new Promise<T>((resolve, reject) => {
      const timer = window.setTimeout(() => {
        this.pending.delete(id);
        reject(new ApiError('Компьютер не ответил вовремя.', 504));
      }, REQUEST_TIMEOUT);
      this.pending.set(id, { resolve, reject, timer });
      // The Mac accepts only strictly increasing counters, and sealing is async:
      // number and send inside one serialized step so frames never overtake each other.
      this.chain = this.chain.then(() =>
        this.send(ws, { k: 'req', sn: this.sn, n: ++this.counter, id, m: method, p: path, b: body ?? null }),
      );
    });
  }
}
